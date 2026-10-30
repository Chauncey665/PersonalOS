"""Pronunciation-only formatting; never changes the saved task or calendar text."""
import re
REVISION='natural-0123'
DIGITS='零一二三四五六七八九'
def number(value):
 parts=str(value).split('.')
 n=int(parts[0])
 if n<10:result=DIGITS[n]
 elif n<100:result=('' if n<20 else DIGITS[n//10])+'十'+(DIGITS[n%10] if n%10 else '')
 elif n<1000:result=DIGITS[n//100]+'百'+(('零' if n%100<10 else '')+number(n%100) if n%100 else '')
 else:result=''.join(DIGITS[int(c)] for c in parts[0])
 return result+('点'+''.join(DIGITS[int(c)] for c in parts[1]) if len(parts)>1 else '')
def natural_text(text):
 if not re.search('[\u4e00-\u9fff]',text):return text
 text=re.sub(r'(\d{1,3}(?:\.\d+)?)\s*[%％]',lambda m:'百分之'+number(m[1]),text)
 text=re.sub(r'(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)',lambda m:number(m[1])+'点'+(number(m[2])+'分' if int(m[2]) else '整'),text)
 text=re.sub(r'(?<!\d)(\d{1,3}(?:\.\d+)?)\s*(公里|米|分钟|小时|摄氏度)(?![A-Za-z])',lambda m:number(m[1])+m[2],text)
 return text
