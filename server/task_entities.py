"""Deterministic, owner-scoped course/task labels. Never let an LLM invent IDs."""
import re

def normalize(text):
    text = re.sub(r'[零〇一二两三四五六七八九]{4}', lambda m: m[0].translate(
        str.maketrans('零〇一二两三四五六七八九', '001223456789')), text.lower())
    text = re.sub(r'(?i)\bhw\s*(\d+)', r'homework\1', text)
    text = re.sub(r'(?i)\b(?:assignment|homework|lab)\s*0*(\d+)', lambda m: m[0].replace(' ', ''), text)
    return re.sub(r'[\s的·_\-:：]+', '', text)

def candidates(items, query):
    q = normalize(query)
    if not q: return []
    exact = [x for x in items if normalize(x['title']) == q]
    if exact: return exact
    codes = re.findall(r'(?<!\d)\d{4}(?!\d)', q)
    pool = list(items)
    for code in codes:
        pool = [x for x in pool if code in re.findall(r'(?<!\d)\d{4}(?!\d)',
                normalize(x['title'] + ' ' + x.get('notes', '').split('\n')[0]))]
    # Course prefix and generic nouns alone must not pick one of several labs.
    rest = re.sub(r'(?:isdn|envr|comp|math)?\d{4}', '', q)
    rest = re.sub(r'^(?:那个|这个|这份|那份|我的)|(?:任务|待办|项目)$', '', rest)
    if rest in ('', '作业', '课程'):
        if not codes: return []
        if rest == '作业':
            assignments = [x for x in pool if re.search(r'assignment|homework|\bhw\b|作业|lab', x['title'], re.I)]
            return assignments or pool
        return pool
    families = [(r'cheatsheet|小抄', r'cheatsheet|小抄'), (r'复习|review|revision', r'复习|review|revision'),
                (r'(?:作业|assignment|homework)([一二三四五六七八九十\d]+)', r'assignment|homework|作业'),
                (r'(?:实验|lab)([一二三四五六七八九十\d]+)', r'lab|实验')]
    for pattern, match in families:
        m = re.search(pattern, rest)
        if m:
            pool = [x for x in pool if re.search(match, normalize(x['title']))]
            if m.lastindex:
                number = m[1].translate(str.maketrans('一二三四五六七八九', '123456789'))
                pool = [x for x in pool if re.search(r'(?<!\d)' + re.escape(number) + r'(?!\d)', normalize(x['title']))]
            return pool
    return [x for x in pool if rest in normalize(x['title'])]
