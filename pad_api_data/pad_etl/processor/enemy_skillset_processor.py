"""
Contains code to convert a list of enemy behavior logic into a flattened structure
called a ProcessedSkillset.
"""
import collections
import copy

import pad_etl.processor.debug_utils
from pad_etl.data.card import BookCard
from .enemy_skillset import *
from typing import List, Any, Set, Tuple, Optional, Dict

# This is a hack that accounts for the fact that some monsters seem to be zero-indexed
# rather than 1-indexed for jumps. Not obvious why this occurs yet.
ZERO_INDEXED_MONSTERS = [
    565,  # Goemon
]


# Everything in this chunk False is the old behavior
# This one is probably wrong, especially if we use SEED_CTX
APPLY_TURN_AROUND_PREEMPT = False
SEED_CTX = False
START_TURN_UPDATE = False
END_TURN_UPDATE = False
USE_SETS_MAX = False


# Both of these as TRUE is the default behavior
USE_UPDATE = not START_TURN_UPDATE and not END_TURN_UPDATE

# Good chance that this should be False if SEED_CTX is true.
UPDATE_USE_IN_PREEMPT = True

SCHEME_1 = False
SCHEME_2 = True


if SCHEME_1:
    SEED_CTX = True
    START_TURN_UPDATE = True
    UPDATE_USE_IN_PREEMPT = False
    USE_SETS_MAX = True


if SCHEME_2:
    SEED_CTX = False
    START_TURN_UPDATE = True
    UPDATE_USE_IN_PREEMPT = True
    USE_SETS_MAX = True


class StandardSkillGroup(object):
    """Base class storing a list of skills."""

    def __init__(self, skills: List[ESAction], hp_threshold):
        # List of skills which execute.
        self.skills = skills
        # The hp threshold that this group executes on, always present, even if 100.
        self.hp = hp_threshold

        self.hp_range = None  # type: Optional[int]

        # Extra notes for skills in this group that could be appended to output.
        # The index of the note corresponds to the index of the skill.
        self.notes = {} # type: Dict[int, str]

    def __eq__(self, other):
        return self.skills == other.skills


class TimedSkillGroup(StandardSkillGroup):
    """Set of skills which execute on a specific turn, possibly with a HP threshold."""

    def __init__(self, turn: int, hp_threshold: int, skills: List[ESAction]):
        super().__init__(skills, hp_threshold)
        # The turn that this group executes on.
        self.turn = turn
        # If set, this group executes over a range of turns
        self.end_turn = None  # type: Optional[int]


class RepeatSkillGroup(TimedSkillGroup):
    """Set of skills which execute on a specific turn, possibly with a HP threshold."""

    def __init__(self, turn: int, interval: int, hp_threshold: int, skills: List[ESAction]):
        super().__init__(turn, hp_threshold, skills)
        # The number of turns between repeats, aka loop size
        self.interval = interval


class HpActions():
    def __init__(self, hp: int, timed: List[TimedSkillGroup], repeating: List[RepeatSkillGroup]):
        self.hp = hp
        self.timed = timed
        self.repeating = repeating


class Moveset(object):
    def __init__(self):
        # Action which triggers when a status is applied.
        self.status_action = None  # type: ESAttackUpStatus
        # Action which triggers player has a buff.
        self.dispel_action = None  # type: ESDispel
        # Timed and repeating actions which execute at various HP checkpoints.
        self.hp_actions = []  # type: List[HpActions]


class EnemyRemainingMoveset(Moveset):
    def __init__(self, count: int):
        super().__init__()
        # Number of enemies required to trigger this set of actions.
        self.count = count


class ProcessedSkillset(object):
    """Flattened set of enemy skills.

    Skills are broken into chunks which are intended to be output independently,
    roughly in the order in which they're declared here.
    """

    def __init__(self, level: int):
        # The monster level this skillset applies to.
        self.level = level
        # Things like color/type resists, resolve, etc.
        self.base_abilities = []  # type: List[ESAction]
        # Preemptive attacks, shields, combo guards.
        self.preemptives = []  # type: List[ESAction]

        # Default enemy actions.
        self.moveset = Moveset()

        # Alternate movesets which execute when a specific number of enemies remain.
        self.enemy_remaining_movesets = []  # type: List[EnemyRemainingMoveset]


class Context(object):
    """Represents the game state when running through the simulator."""

    def __init__(self, level):
        self.turn = 1
        # Whether the current turn triggered a preempt flag.
        self.is_preemptive = False
        # Whether we are allowed to preempt based on level flags.
        self.do_preemptive = False
        # A bitmask for flag values which can be updated.
        self.flags = 0
        # A bitmask for flag values which can only be unset.
        self.skill_use = 0
        # Special flag that is modified by the 'counter' operations.
        self.counter = 0
        # Countdown on/off
        self.countdown = False
        # Current HP value.
        self.hp = 100
        # Monster level, toggles some behaviors.
        self.level = level
        # Number of enemies on the screen.
        self.enemies = 999
        # Cards on the team.
        self.cards = set()
        # Turns of enrage, initial:None -> (enrage cooldown period:int<0 ->) enrage:int>0 -> expire:int=0
        self.enraged = None
        # Turns of damage shield, initial:int=0 -> shield up:int>0 -> expire:int=0
        self.damage_shield = 0
        # Turns of status shield, initial:int=0 -> shield up:int>0 -> expire:int=0
        self.status_shield = 0
        # Turns of combo shield, initial:int=0 -> shield up:int>0 -> expire:int=0
        self.combo_shield = 0

    def reset(self):
        self.is_preemptive = False

    def clone(self):
        return copy.deepcopy(self)

    def check_skill_use(self, usage):
        raise NotImplementedError('check_skill_use')

    def update_skill_use(self, usage):
        raise NotImplementedError('update_skill_use')

    def turn_event(self):
        self.turn += 1
        if self.enraged is not None:
            if self.enraged > 0:
                # count down enraged turns
                self.enraged -= 1
            elif self.enraged < 0:
                # count up enraged cooldown turns
                self.enraged += 1
        if self.damage_shield > 0:
            # count down shield turns
            self.damage_shield -= 1
        if self.status_shield > 0:
            # count down shield turns
            self.status_shield -= 1
        if self.combo_shield > 0:
            # count down shield turns
            self.combo_shield -= 1
        if self.countdown:
            if self.counter > 0:
                self.counter -= 1
            else:
                self.countdown = False

    def apply_skill_effects(self, behavior):
        """Check context to see if a skill is allowed to be used, and update flag accordingly"""
        b_type = type(behavior)
        if issubclass(b_type, ESAttackUp):
            if b_type == ESAttackUPRemainingEnemies \
                    and behavior.enemy_count is not None \
                    and self.enemies > behavior.enemy_count:
                return False
            if self.enraged is None:
                if b_type == ESAttackUPCooldown and behavior.turn_cooldown is not None:
                    self.enraged = -behavior.turn_cooldown + 1
                    return False
                else:
                    self.enraged = behavior.turns
                    return True
            else:
                if self.enraged == 0:
                    self.enraged = behavior.turns
                    return True
                else:
                    return False
        elif b_type == ESDamageShield:
            if self.damage_shield == 0:
                self.damage_shield = behavior.turns
                return True
            else:
                return False
        elif b_type == ESStatusShield:
            if self.status_shield == 0:
                self.status_shield = behavior.turns
                return True
            else:
                return False
        elif b_type == ESAbsorbCombo:
            if self.combo_shield == 0:
                self.combo_shield = behavior.max_turns
                return True
            else:
                return False
        # TODO: Delete this. I don't think it makes sense to update the HP in response
        # to an enemy action; we evaluate at the end of the player's round.
        # elif b_type == ESRecoverEnemy:
        #     self.hp += behavior.max_amount
        return True

    def start_turn(self):
        pass


class CTXBitmap(Context):
    def __init__(self, level, skill_use_flags):
        # TODO: skill_use_flags param might be useless
        super(CTXBitmap, self).__init__(level)
        self.skill_use = 0

    def check_skill_use(self, usage):
        return usage is None or self.skill_use & usage == 0

    def update_skill_use(self, usage):
        if usage is not None:
            self.skill_use |= usage


class CTXCounter(Context):
    def __init__(self, level, skill_use_counter):
        # TODO: skill_use_counter param might be useless
        super(CTXCounter, self).__init__(level)
        self.skill_use_counter = skill_use_counter
        self.skill_use = skill_use_counter if SEED_CTX else 0

    def check_skill_use(self, usage):
        if usage is None:
            return True
        else:
            if self.skill_use == 0:
                return True
            elif self.skill_use > 0:
                if USE_UPDATE:
                    self.skill_use -= 1
                return False

    def update_skill_use(self, usage):
        if usage is not None:
            if USE_SETS_MAX:
                self.skill_use = self.skill_use_counter
            else:
                self.skill_use += usage

    def start_turn(self):
        super().start_turn()
        if START_TURN_UPDATE:
            self.skill_use -= 1

    def turn_event(self):
        super().turn_event()
        if END_TURN_UPDATE:
            self.skill_use -= 1


def default_attack():
    """Indicates that the monster uses its standard attack."""
    return ESDefaultAttack()


def loop_through(ctx, behaviors: List[ESBehavior]) -> List[ESBehavior]:
    """Executes a single turn through the simulator.

    This is called multiple times with varying Context values to probe the action set
    of the monster.
    """
    ctx.reset()
    # The list of behaviors identified for this loop.
    results = []
    # A list of behaviors which have been iterated over.
    traversed = []

    # The current spot in the behavior array.
    idx = 0
    # Number of iterations we've done.
    iter_count = 0
    while iter_count < 1000:
        # Safety measures against infinite loops, check if we've looped too many
        # times or if we've seen this behavior before in the current loop.
        iter_count += 1
        if idx >= len(behaviors) or idx in traversed:
            # Disabling default action for now; doesn't seem to improve things?
            # if len(results) == 0:
            #     # if the result set is empty, add something
            #     results.append(default_attack())
            return results
        traversed.append(idx)

        # Extract the current behavior and its type.
        b = behaviors[idx]
        b_type = type(b)

        # The current action could be None because we nulled it out in preprocessing, just continue.
        if b is None or b_type == ESNone:
            idx += 1
            continue

        # Detection for preempts, null the behavior afterwards so we don't trigger it again.
        if b_type == ESPreemptive:
            behaviors[idx] = None
            ctx.is_preemptive = True
            ctx.do_preemptive = b.level <= ctx.level
            idx += 1
            continue

        if b_type == ESAttackPreemptive:
            behaviors[idx] = None
            ctx.is_preemptive = True
            ctx.do_preemptive = True
            results.append(b)
            if UPDATE_USE_IN_PREEMPT:
                ctx.update_skill_use(b.condition.one_time)
            return results

        if b_type == ESCountdown:
            ctx.countdown = True
            if ctx.counter == 1:
                idx += 1
                continue
            else:
                results.append(b)
                return results

        if b_type == ESAttackUpStatus:
            # This is a special case; it's not a terminal action unlike other enrages.
            results.append(b)
            idx += 1
            continue

        # Processing for actions and unparsed stuff, this section should accumulate
        # items into results.
        if b_type == EnemySkillUnknown or issubclass(b_type, ESAction):
            # Check if we should execute this action at all.
            if skill_has_condition(b):
                cond = b.condition
                # HP based checks.
                if cond.hp_threshold and ctx.hp >= cond.hp_threshold:
                    idx += 1
                    continue

                if cond.use_chance() == 100 and b_type != ESDispel:
                    # This always executes so it is a terminal action.
                    if not ctx.check_skill_use(cond.one_time):
                        idx += 1
                        continue
                    if not ctx.apply_skill_effects(b):
                        idx += 1
                        continue
                    ctx.update_skill_use(cond.one_time)
                    results.append(b)
                    return results
                else:
                    # Not a terminal action, so accumulate it and continue.
                    if ctx.check_skill_use(cond.one_time) and ctx.apply_skill_effects(b):
                        results.append(b)
                    idx += 1
                    continue
            else:
                # Stuff without a condition is always terminal.
                if not ctx.apply_skill_effects(b):
                    idx += 1
                    continue
                return results

        if b_type == ESBranchFlag:
            if b.branch_value == b.branch_value & ctx.flags:
                # If we satisfy the flag, branch to it.
                idx = b.target_round
            else:
                # Otherwise move to the next action.
                idx += 1
            continue

        if b_type == ESEndPath:
            # Forcibly ends the loop, generally used after multiple <100% actions.
            # Disabling default action for now; doesn't seem to improve things?
            # if len(results) == 0:
            #     # if the result set is empty, add something
            #     results.append(default_attack())
            return results

        if b_type == ESFlagOperation:
            # Operations which change flag state, we always move to the next behavior after.
            if b.operation == 'SET' or b.operation == 'OR':
                # This is a bit suspicious that they have both SET and OR, possibly
                # these should be broken apart?
                ctx.flags = ctx.flags | b.flag
            elif b.operation == 'UNSET':
                ctx.flags = ctx.flags & ~b.flag
            else:
                raise ValueError('unsupported flag operation:', b.operation)
            idx += 1
            continue

        if b_type == ESBranchHP:
            # Branch based on current HP.
            if b.compare == '<':
                take_branch = ctx.hp < b.branch_value
            else:
                take_branch = ctx.hp >= b.branch_value
            idx = b.target_round if take_branch else idx + 1
            continue

        if b_type == ESBranchLevel:
            # Branch based on monster level.
            if b.compare == '<':
                take_branch = ctx.level < b.branch_value
            else:
                take_branch = ctx.level >= b.branch_value
            idx = b.target_round if take_branch else idx + 1
            continue

        if b_type == ESSetCounter:
            # Adjust the global counter value.
            if b.set == '=':
                ctx.counter = b.counter
            elif b.set == '+':
                ctx.counter += b.counter
            elif b.set == '-':
                ctx.counter -= b.counter
            idx += 1
            continue

        if b_type == ESSetCounterIf:
            # Adjust the counter if it has a specific value.
            if ctx.counter == b.counter_is:
                ctx.counter = b.counter
            idx += 1
            continue

        if b_type == ESBranchCounter:
            # Branch based on the counter value.
            if b.compare == '=':
                take_branch = ctx.counter == b.branch_value
            elif b.compare == '<':
                take_branch = ctx.counter <= b.branch_value
            elif b.compare == '>':
                take_branch = ctx.counter >= b.branch_value
            else:
                raise ValueError('unsupported counter operation:', b.compare)
            idx = b.target_round if take_branch else idx + 1
            continue

        if b_type == ESBranchCard:
            # Branch if it's checking for a card we have on the team.
            card_on_team = any([card in ctx.cards for card in b.branch_value])
            idx = b.target_round if card_on_team else idx + 1
            continue

        if b_type == ESBranchRemainingEnemies:
            if ctx.enemies == b.branch_value:
                idx = b.target_round
            else:
                idx += 1
            continue

        raise ValueError('unsupported operation:', b_type, b)

    if iter_count == 1000:
        print('error, iter count exceeded 1000')
    return results


def info_from_behaviors(behaviors: List[ESBehavior]):
    """Extract some static info from the behavior list and clean it up where necessary."""
    base_abilities = []
    hp_checkpoints = set()
    hp_checkpoints.add(100)
    hp_checkpoints.add(0)
    card_checkpoints = set()
    has_enemy_remaining_branch = False

    for idx, es in enumerate(behaviors):
        # Extract the passives and null them out to simplify processing
        if issubclass(type(es), ESPassive):
            base_abilities.append(es)
            behaviors[idx] = None

        # Find candidate branch HP values
        if type(es) == ESBranchHP:
            hp_checkpoints.add(es.branch_value)
            hp_checkpoints.add(es.branch_value - 1)

        # Find candidate action HP values
        if skill_has_condition(es):
            cond = es.condition
            if cond and cond.hp_threshold:
                hp_checkpoints.add(cond.hp_threshold)
                hp_checkpoints.add(cond.hp_threshold - 1)

        # Find checks for specific cards.
        if type(es) == ESBranchCard:
            card_checkpoints.update(es.branch_value)

        # Find checks for specific amounts of enemies.
        if type(es) == ESBranchRemainingEnemies or type(es) == ESAttackUPRemainingEnemies:
            has_enemy_remaining_branch = True

    return base_abilities, hp_checkpoints, card_checkpoints, has_enemy_remaining_branch


def extract_preemptives(ctx: Context, behaviors: List[Any]):
    """Simulate the initial run through the behaviors looking for preemptives.

    If we find a preemptive, continue onwards. If not, roll the context back.
    """
    original_ctx = ctx.clone()

    if APPLY_TURN_AROUND_PREEMPT:
        ctx.start_turn()
    cur_loop = loop_through(ctx, behaviors)
    if APPLY_TURN_AROUND_PREEMPT:
        ctx.turn_event()

    if ctx.is_preemptive:
        # Save the current loop as preempt
        return ctx, cur_loop
    else:
        # Roll back the context.
        return original_ctx, None


def extract_turn_behaviors(ctx: Context, behaviors: List[ESBehavior], hp_checkpoint: int) -> List[List[ESBehavior]]:
    """Simulate the first 20 turns at a specific hp checkpoint."""
    hp_ctx = ctx.clone()
    hp_ctx.hp = hp_checkpoint
    turn_data = []
    for idx in range(0, 20):
        ctx.start_turn()
        turn_data.append(loop_through(hp_ctx, behaviors))
        ctx.turn_event()

    return turn_data


def extract_loop_indexes(turn_data: List[ESBehavior]) -> Tuple[int, int]:
    """Find loops in the data."""
    # Loop over every turn
    for i_idx, check_data in enumerate(turn_data):
        # Loop over every following turn. If the outer turn matches an inner turn moveset,
        # we found a loop.
        possible_loops = []
        for j_idx in range(i_idx + 1, len(turn_data)):
            comp_data = turn_data[j_idx]
            if check_data == comp_data:
                possible_loops.append((i_idx, j_idx))
        if len(possible_loops) == 0:
            continue

        # Check all possible loops
        for check_start, check_end in possible_loops.copy():
            # Now that we found a loop, confirm that it continues
            loop_behavior = turn_data[check_start:check_end]
            loop_verified = False

            for j_idx in range(check_end, len(turn_data), len(loop_behavior)):
                # Check to make sure we don't run over the edge of the array
                j_loop_end_idx = j_idx + len(loop_behavior)
                if j_loop_end_idx > len(turn_data):
                    # We've overlapped the end of the array with no breaks, quit
                    break

                comp_data = turn_data[j_idx:j_loop_end_idx]
                loop_verified = loop_behavior == comp_data
                if not loop_verified:
                    break

            if not loop_verified:
                # The loop didn't continue so this is a bad selection, remove
                possible_loops.remove((check_start, check_end))

        if len(possible_loops) > 0:
            return possible_loops[0][0], possible_loops[0][1]

    raise Exception('No loop found')


def extract_loop_skills(hp: int, turn_data: list, loop_start: int, loop_end: int) -> HpActions:
    """Create timed and repeating skill groups."""
    timed_skill_groups = []
    if loop_start > 0:
        for idx in range(0, loop_start):
            timed_skill_groups.append(TimedSkillGroup(idx + 1, hp, turn_data[idx]))

    loop_size = loop_end - loop_start
    repeating_skill_groups = []
    for idx in range(loop_start, loop_end):
        repeating_skill_groups.append(RepeatSkillGroup(idx + 1, loop_size, hp, turn_data[idx]))

    return HpActions(hp, timed_skill_groups, repeating_skill_groups)


def compute_enemy_actions(ctx: Context, behaviors: List[ESBehavior], hp_checkpoints: List[int]) -> List[HpActions]:
    # Compute turn behaviors for every hp checkpoint
    hp_to_turn_behaviors = {hp: extract_turn_behaviors(ctx, behaviors, hp) for hp in hp_checkpoints}

    # Convert turn behaviors into fixed turns and repeating loops.
    hp_to_actions = {}  # type Map[int, HpActions]
    for hp, turn in hp_to_turn_behaviors.items():
        loop_start, loop_end = extract_loop_indexes(turn)
        hp_to_actions[hp] = extract_loop_skills(hp, turn, loop_start, loop_end)

    # Starting from the top hp bracket and extending down, compute the true timed actions.
    for chp_idx in range(len(hp_checkpoints)):
        # Extract the hp, and timed actions for the current index.
        hp = hp_checkpoints[chp_idx]
        actions = hp_to_actions[hp]
        timed = actions.timed
        # Loop over every timed action.
        for turn_idx in range(len(timed)):
            cur_skills = timed[turn_idx].skills
            # Loop over every subsequent hp checkpoint
            for nhp_idx in range(chp_idx + 1, len(hp_checkpoints)):
                comp_hp = hp_checkpoints[nhp_idx]
                comp_actions = hp_to_actions[comp_hp]
                # Check if the checkpoint has a timed action at that slot, and
                # if it is equivalent. If so, smear the parent to that hp and
                # delete the child.
                # This takes care of 'true' timed actions (actions which always execute on turn n)
                # as well as hp thresholded actions that span checkpoints.
                comp_timed = comp_actions.timed
                if turn_idx >= len(comp_timed):
                    # This timed set had less slots than the parent, we're done.
                    break

                if cur_skills == comp_timed[turn_idx].skills:
                    comp_timed[turn_idx].skills.clear()
                    timed[turn_idx].hp_range = comp_hp

        repeating = actions.repeating
        # Loop over the repeating action set at each checkpoint. If the whole repeating action
        # set is equivalent, smear it.
        for nhp_idx in range(chp_idx + 1, len(hp_checkpoints)):
            comp_hp = hp_checkpoints[nhp_idx]
            comp_actions = hp_to_actions[comp_hp]
            comp_repeating = comp_actions.repeating
            if [x.skills for x in repeating] == [x.skills for x in comp_repeating]:
                for x in repeating:
                    # TODO: maybe repeating should be an object with a hp slot instead
                    x.hp_range = comp_hp
                comp_repeating.clear()

    return list(hp_to_actions.values())


def convert(card: BookCard, enemy_behavior: List[ESBehavior],
            level: int, enemy_skill_effect: int, enemy_skill_effect_type: int, force_one_enemy: bool=False):
    skillset = ProcessedSkillset(level)

    # Behavior is 1-indexed, so stick a fake row in to start
    behaviors = [None] + list(enemy_behavior)  # type: List[ESBehavior]

    # Fix some monsters that seem to be 0-indexed
    if card.card_id in ZERO_INDEXED_MONSTERS:
        behaviors.pop(0)

    base_abilities, hp_checkpoints, card_checkpoints, has_enemy_remaining_branch = info_from_behaviors(
        behaviors)
    skillset.base_abilities = base_abilities

    # Ensure the HP checkpoints are in descended order
    hp_checkpoints = sorted(hp_checkpoints, reverse=True)

    # Pick the correct enemy_skill_effect model to use
    if enemy_skill_effect_type == 0:
        ctx = CTXBitmap(level, enemy_skill_effect)
    elif enemy_skill_effect_type == 1:
        ctx = CTXCounter(level, enemy_skill_effect)
    else:
        # ctx = Context(level)
        # For now fall back to the old context implementation to prevent errors in log.
        print('Incorrect context used')
        ctx = CTXBitmap(level, enemy_skill_effect)

    if force_one_enemy:
        ctx.enemies = 1
        has_enemy_remaining_branch = False

    ctx, preemptives = extract_preemptives(ctx, behaviors)
    if ctx is None:
        # Some monsters have no skillset at all
        return skillset

    if preemptives is not None:
        skillset.preemptives = preemptives
        if any([p.ends_battle() for p in preemptives]):
            # This monster terminates the battle immediately.
            return skillset

    # Compute the standard action moveset
    hp_actions = compute_enemy_actions(ctx.clone(), behaviors, hp_checkpoints)
    clean_skillset(skillset.moveset, hp_actions)

    # Simulate enemies being defeated
    if has_enemy_remaining_branch:
        enemy_movesets = []
        # Loop from 6 back to 1, simulating possible movesets.
        for ecount in range(6, 0, -1):
            enemy_moveset = EnemyRemainingMoveset(ecount)
            enemy_ctx = ctx.clone()
            enemy_ctx.enemies = ecount
            enemy_actions = compute_enemy_actions(enemy_ctx, behaviors, hp_checkpoints)
            clean_skillset(enemy_moveset, enemy_actions)
            enemy_movesets.append(enemy_moveset)

        # Dedupe the enemy remaining movesets, with the 'standard' moveset as the
        # initial state. We only want to keep deltas from that state.
        all_movesets = [skillset.moveset] + enemy_movesets
        for cms_idx in range(len(all_movesets)):
            cms = all_movesets[cms_idx]

            for nms_idx in range(cms_idx + 1, len(all_movesets)):
                nms = all_movesets[nms_idx]

                if cms.dispel_action == nms.dispel_action:
                    nms.dispel_action = None
                if cms.status_action == nms.status_action:
                    nms.status_action = None

                for cms_action in cms.hp_actions:
                    nms_action = find_action_by_hp(cms_action.hp, nms.hp_actions)
                    if nms_action is None:
                        continue

                    if cms_action.timed == nms_action.timed:
                        nms_action.timed.clear()
                    if cms_action.repeating == nms_action.repeating:
                        nms_action.repeating.clear()

        for moveset in enemy_movesets:
            moveset.hp_actions = [x for x in moveset.hp_actions if x.timed or x.repeating]
            if moveset.hp_actions:
                skillset.enemy_remaining_movesets.append(moveset)

    return skillset


def collapse_repeating_groups(groups: List[TimedSkillGroup]) -> List[TimedSkillGroup]:
    """For repeating movesets, collapse consecutive repeats."""
    if len(groups) <= 1:
        # No work we can do here
        return groups

    cur_item = groups[0]
    new_groups = [cur_item]
    for idx in range(1, len(groups)):
        next_item = groups[idx]
        if cur_item.skills == next_item.skills and cur_item.turn != next_item.turn:
            cur_item.end_turn = next_item.turn
        else:
            new_groups.append(next_item)
            cur_item = next_item
    return new_groups


def clean_skillset(moveset: Moveset, hp_actions: List[HpActions]):
    """Perform a variety of cleanups and compute the final skillset."""

    def extract_and_clear_action(skills, action_type):
        """Clears an action by type from the skill list and returns any matches."""
        actions = [s for s in skills if type(s) == action_type]
        skills[:] = [s for s in skills if type(s) != action_type]
        return actions

    # Extract special hoisted actions if present, clearing them in their home sets.
    status_skills = []
    dispel_skills = []
    for hp_action in hp_actions:
        for t in hp_action.timed:
            status_skills.extend(extract_and_clear_action(t.skills, ESAttackUpStatus))
            dispel_skills.extend(extract_and_clear_action(t.skills, ESDispel))

        for r in hp_action.repeating:
            status_skills.extend(extract_and_clear_action(r.skills, ESAttackUpStatus))
            dispel_skills.extend(extract_and_clear_action(r.skills, ESDispel))

    # If we found anything, set them. These are lists but they should just contain the
    # same item repeatedly, unless a monster has multiple status-enrage/dispels?
    if status_skills:
        moveset.status_action = status_skills[0]
    if dispel_skills:
        moveset.dispel_action = dispel_skills[0]

    # Collapse unnecessary outputs
    for hp_action in hp_actions:
        hp_action.timed = collapse_repeating_groups(hp_action.timed)
        hp_action.repeating = collapse_repeating_groups(hp_action.repeating)

    # Only add non-empty hp checkpoints
    for hp_action in hp_actions:
        # Make sure repeating groups have at least one skill in them.
        hp_action.timed = [x for x in hp_action.timed if x.skills]
        hp_action.repeating = [x for x in hp_action.repeating if x.skills]
        if hp_action.timed or hp_action.repeating:
            moveset.hp_actions.append(hp_action)


def extract_levels(enemy_behavior: List[Any]):
    """Scan through the behavior list and compile a list of level values, always including 1."""
    levels = set()
    levels.add(1)
    for b in enemy_behavior:
        if type(b) == ESBranchLevel:
            levels.add(b.branch_value)
        elif hasattr(b, 'level'):
            levels.add(b.level)
    return levels


def skill_has_condition(es):
    if not hasattr(es, 'condition'):
        return False
    return es.condition is not None

def find_action_by_hp(hp: int, actions: List[HpActions]) -> Optional[HpActions]:
    for a in actions:
        if a.hp == hp:
            return a
    return None
