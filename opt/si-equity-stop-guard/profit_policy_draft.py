from decimal import Decimal
VERSION = 'equity-profit-policy-draft-v1'

def number(value):
    if isinstance(value, bool):
        raise ValueError('Boolean is not a price or percentage')
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError('Nonfinite number')
    return result

def evaluate(*, entry, bid, settings, previous=None, score=None, score_fresh=False):
    entry, bid = (number(entry), number(bid))
    if entry <= 0 or bid <= 0:
        raise ValueError('Nonpositive price')
    stop, start, goal, floor = (number(settings[key]) for key in ('stop_loss_pct', 'profit_start_pct', 'profit_goal_pct', 'profit_floor_pct'))
    if not (0 < stop < 100 and 0 < start <= goal <= floor):
        raise ValueError('Invalid policy settings')
    state = {'protection_armed': False, 'floor_armed': False}
    if previous is not None:
        if not isinstance(previous, dict) or set(previous) != set(state) or any((type(v) is not bool for v in previous.values())) or (previous['floor_armed'] and (not previous['protection_armed'])):
            raise ValueError('Invalid milestone state')
        state.update(previous)
    hundred = Decimal('100')
    activation_price = entry * (1 + start / hundred)
    goal_price = entry * (1 + goal / hundred)
    floor_price = entry * (1 + floor / hundred)
    loss_price = entry * (1 - stop / hundred)
    if bid >= activation_price:
        state['protection_armed'] = True
    if bid > floor_price:
        state['floor_armed'] = True
    valid_score = type(score) is int and 0 <= score <= 10 and (score_fresh is True)
    action, trigger = ('HOLD', None)
    if bid <= loss_price:
        action, trigger = ('EXIT_LOSS_STOP', loss_price)
    elif state['floor_armed'] and bid <= floor_price:
        action, trigger = ('EXIT_PROFIT_FLOOR', floor_price)
    elif state['protection_armed'] and bid <= entry:
        action, trigger = ('EXIT_ENTRY_PRICE', entry)
    elif bid >= goal_price:
        if not valid_score:
            action, trigger = ('EXIT_SCORE_UNAVAILABLE', goal_price)
        elif score < 6:
            action, trigger = ('EXIT_WEAK_RALLY', goal_price)
        else:
            action = 'HOLD_STRONG_RALLY'
    return {'action': action, 'state': state, 'trigger_price': str(trigger) if trigger is not None else None, 'score_required': bid >= goal_price and (not action.startswith('EXIT_'))}
