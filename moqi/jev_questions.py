"""Fixed output domains. Policy updates may change wording, never answer IDs."""
INTENTS = {
    'constraint':'明确要求限制系统自动操作设备，包括临时或长期禁止',
    'preference':'明确表达或修改工作、睡觉等情境下喜欢的空调设定温度',
    'exception':'说明选中的过去行为是临时特例，不应学成日常习惯',
    'attribution':'明确更正选中事件的来源，是人操作还是既有自动化',
    'explain':'询问系统某个操作或决定的原因',
    'memory_changes':'询问系统最近学到什么或记忆发生了哪些变化',
    'unknown':'其他、含糊、引用例句、撤回要求，或没有明确要求修改记忆',
}
SCOPES = {
    'temporary':'仅这一次夜晚、今晚或今天晚上，之后恢复原来的安排',
    'night':'以后每晚或夜里都这样，没有限定工作日',
    'weekday_night':'只在工作日或上班日的夜晚这样，休息日照旧',
    'always':'始终或以后都这样，没有时间限制',
    'unknown':'时间要求互相矛盾，或需要其他尚不支持的时段',
}
QUESTIONS = {
    'intent':{'type':'choice','instructions':'按用户这句话实际表达的请求分类。引用、撤回、推测或不明确的要求选 unknown。','criteria':INTENTS},
    'scope':{'type':'choice','instructions':'这项限制在哪些时候有效？区分一次性今晚与长期每晚。','criteria':SCOPES},
    'action':{'type':'choice','instructions':'用户不允许系统自动做哪项设备动作？',
              'criteria':{'turn_on':'开启设备','turn_off':'关闭设备','unknown':'动作未说明或不是开关动作'}},
    'activity':{'type':'choice','instructions':'用户明确说在哪一种活动期间采用这个温度偏好？',
                'criteria':{'working':'工作、办公或干活期间','sleeping':'睡觉或睡眠期间','unknown':'没有说明、同时说多种活动或是其他活动'}},
    'source':{'type':'choice','instructions':'这句话明确指出事件由谁造成？',
              'criteria':{'human':'人亲手操作','device':'既有自动化或设备定时触发','unknown':'没有明确说明是谁造成'}},
}
