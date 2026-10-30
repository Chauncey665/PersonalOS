"""PersonalOS-owned implementation of lazy tools, bounded context and no-progress guards.

Design references and pinned source revisions: THIRD_PARTY_NOTICES.md.
No upstream implementation or runtime dependency is bundled here.
"""
import copy
import json
import re

GROUPS = {
    'tasks': ('resolve_todos','record_todos','update_todo_complete','add_todo_children','list_scheduled_todos','reschedule_unfinished','plan_todos','fill_reminder_time'),
    'files': ('search_documents','files','prepare_organization'),
    'documents': ('read_document','edit_document'),
    'live': ('weather','public_holidays','web_search'),
    'memory': ('remember_preference','forget_preference'),
}
BASE = {'device_status','task_history','today_calendar','list_todos','load_tools'}
PATTERNS = {
    'tasks': r'待办|任务|作业|截止|日程|日历|排程|安排|提醒|完成|项目|计划|task|todo|schedule|deadline|remind|plan',
    'files': r'文件|资料|目录|桌面|整理|PROJECT|文档|file|folder|document|search',
    'live': r'天气|下雨|气温|节日|放假|联网|搜索|weather|rain|holiday|web|search',
    'memory': r'记住|忘记|偏好|习惯|remember|forget|preference',
}

class ToolSurface:
    def __init__(self, tools, text, documents=False, readonly=False, memory=False, unavailable=()):
        self.tools=tools
        self.allowed={t['function']['name'] for t in tools} - set(unavailable)
        if not memory:self.allowed.discard('remember_preference')
        if documents:
            self.allowed &= {'read_document','edit_document','load_tools'} | ({'web_search'} if re.search(r'搜索|查资料|联网|research|search',text,re.I) else set())
        elif readonly:
            self.allowed &= {'weather','public_holidays','web_search','today_calendar','list_todos','task_history','list_scheduled_todos','device_status','load_tools'}
        self.active=set(BASE) & self.allowed
        if documents:self.active |= set(GROUPS['documents']) & self.allowed
        else:
            for group,pattern in PATTERNS.items():
                if re.search(pattern,text,re.I):self.active |= set(GROUPS[group]) & self.allowed
    def schemas(self):
        return [t for t in self.tools if t['function']['name'] in self.active]
    def load(self,args):
        if not isinstance(args,dict) or set(args)!={'group'} or args['group'] not in GROUPS:
            raise ValueError('请选择工具目录中的一个有效分组')
        names=set(GROUPS[args['group']]) & self.allowed
        if not names:raise ValueError('此分组不在当前请求允许的范围内')
        self.active |= names
        return {'group':args['group'],'loaded':sorted(names),'note':'仅加载工具说明，不代表获得执行权限；下一轮才可调用。'}


def pack(value):return json.dumps(value,ensure_ascii=False,separators=(',',':'))


def compile_context(context,past,system,tools,num_ctx):
    """Keep current request and structured state, then recent verbatim excerpts.

    Character budget is a conservative heuristic, not a tokenizer measurement.
    A digest is ephemeral conversation data, never a saved personal preference.
    """
    value=copy.deepcopy(context)
    # Bound each collection independently so a large calendar cannot erase tasks/history.
    inv=value.get('task_inventory',{})
    while inv.get('items') and len(pack(inv))>4200:
        inv['items'].pop();inv['truncated']=True
    cal=value.get('calendar_context',{})
    if len(pack(cal))>4200:
        value['calendar_context']={'available':False,'reason':'日历快照超过上下文预算，请用today_calendar读取；不能据此推断没有安排。'}
    while value.get('preferences') and len(pack(value['preferences']))>1800:value['preferences'].pop()
    fixed=len(system)+len(pack(tools));budget=max(6000,min(24000,(num_ctx-2304)*2-fixed))
    # Keep the data envelope parseable. Avoid truncating the request into an excerpt.
    for key in ('resources','canvas_status'):
        if len(pack(value))>budget-2400 and key in value:
            value[key]={'truncated':True,'note':'使用相应工具核对完整资料'}
    while inv.get('items') and len(pack(value))>budget-1800:
        inv['items'].pop();inv['truncated']=True
    history_budget=max(0,min(6000,budget-len(pack(value))-800))
    selected=[];digest=[]
    for item in past:
        clean={k:item.get(k) for k in ('id','text','result','state','due')}
        size=len(pack(clean))+100
        if len(selected)<4 and size<=history_budget:
            selected.append(clean);history_budget-=size
        else:
            # Each excerpt retains its source ID and system execution state.
            short={'task_id':item['id'],'request_excerpt':item['text'][:160],
                   'reply_excerpt':item['result'][:180],'state':item['state'],'excerpt':True}
            size=len(pack(short))+30
            if size<=history_budget:digest.append(short);history_budget-=size
    if digest:value['earlier_turn_excerpts']={'items':list(reversed(digest)),'note':'同会话原文摘录，可能不完整；不是长期偏好，也不是本轮执行回执。'}
    messages=[{'role':'system','content':system},{'role':'user','content':pack(value)}]
    for item in reversed(selected):
        messages += [{'role':'user','content':item['text']},{'role':'assistant','content':pack({'task_id':item['id'],'state':item['state'],'due':item['due'],'reply':item['result']})}]
    if past:messages.append({'role':'user','content':context['request']})
    stats={'context_characters':len(pack(value)),'history_turns':len(selected),'history_excerpts':len(digest),
           'tool_schema_characters':len(pack(tools)),'exposed_tools':len(tools),'budget_kind':'character_heuristic'}
    return messages,stats


class ProgressGuard:
    """Cache successes AND failures in this run. Detect A/A and A/B/A/B loops."""
    def __init__(self):self.results={};self.keys=[];self.repeats=0;self.force_answer=False
    def run(self,name,args,execute,available=True):
        key=json.dumps([name,args],ensure_ascii=False,sort_keys=True);self.keys.append(key)
        if available and self.results.get(key,{}).get('error_code')=='tool_not_exposed':self.results.pop(key)
        reused=key in self.results
        if reused:
            self.repeats+=1
            result=copy.deepcopy(self.results[key])
            result['reused_receipt']=True
            result['note']='相同请求已核对，未再次执行；请依据已有回执回答，或更改必要参数。'
        else:
            result=execute();self.results[key]=copy.deepcopy(result)
        if self.repeats>=2:self.force_answer=True
        return result,reused
