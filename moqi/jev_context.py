"""Fit whole facts into the pinned laya layout without truncating an utterance.

The supported EdgeJev revision uses per_question_row, a 48-token option limit,
head_max_len and max_len. Check before system_one, whose upstream layout would
otherwise silently truncate descriptions, instructions and state.
"""
import json


class ContextLimit(ValueError):
    pass


def bounded_state(agent,text,facts,questions):
    config=getattr(agent,'cfg',{})
    encoder=getattr(agent,'enc',None)
    # Protocol fixture agents have no tokenizer. Their estimate is conservative
    # and is explicitly distinguished from actual tokenizer counts.
    count=(lambda value:len(encoder.ids(value))) if encoder is not None else (lambda value:len(value.encode('utf-8')))
    maximum=config.get('max_len',1024)
    head_maximum=config.get('head_max_len',256)
    room=maximum
    marker=config.get('mask_token','<mask>')
    for question in questions.values():
        instructions=question['instructions'].replace(marker,' ')
        head=count('choice question: '+instructions)
        options=[]
        for key,description in question['criteria'].items():
            rendered=' '+key+': '+description.replace(marker,' ')
            size=count(rendered)
            if size>48: raise ContextLimit('JEV answer description exceeds its token budget')
            options.append(1+size)
        if head_maximum-sum(options)<16 or head+sum(options)>head_maximum:
            raise ContextLimit('JEV instructions exceed the question token budget')
        room=min(room,maximum-(1+head+1+sum(options)+1)-1)
    def serialize(value):
        return value if isinstance(value,str) else json.dumps(value,ensure_ascii=False)
    state=text if not facts else dict(utterance=text,household_habits=[])
    if count(serialize(state).replace(marker,' '))>room:
        raise ContextLimit('The full utterance does not fit the JEV context')
    selected=[]
    for fact in facts:
        trial=dict(utterance=text,household_habits=selected+[fact])
        if count(serialize(trial).replace(marker,' '))<=room: selected.append(fact)
    if facts: state=dict(utterance=text,household_habits=selected)
    return state,selected,dict(state_limit=room,state_size=count(serialize(state).replace(marker,' ')),
        count_method='tokenizer' if encoder is not None else 'utf8_upper_estimate',
        habits_available=len(facts),habits_used=len(selected),utterance_truncated=False)
