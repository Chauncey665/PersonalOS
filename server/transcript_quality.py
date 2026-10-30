"""Conservative ASR repetition guard; rejects output, never guesses replacements.

This detects a few extreme decoder failure patterns, not transcript correctness.
It deliberately exempts numeric repetitions, including Chinese spoken digits.
"""
import re
import zlib


def _number(character):
    return character.isnumeric() or character == '两'


def transcript_problem(text):
    """Return a fixed, non-private reason code, or None for no detected pattern."""
    if not isinstance(text,str): return 'invalid_transcript'
    compact=''.join(character.casefold() for character in text if character.isalnum())
    for match in re.finditer(r'([\u3400-\u4dbf\u4e00-\u9fff])\1{7,}',compact):
        if not _number(match[1]): return 'repeated_character'
    # Each width makes at most len(text) comparisons across all phase offsets;
    # avoid a backtracking regex or an unbounded quadratic repeated-substring scan.
    for width in range(2,13):
        for offset in range(width):
            previous=None; count=0
            for index in range(offset,len(compact)-width+1,width):
                fragment=compact[index:index+width]
                valid=fragment.isalpha() and not all(_number(character) for character in fragment)
                if valid and fragment==previous: count+=1
                else: previous=fragment if valid else None; count=1 if valid else 0
                if count>=5 and count*width>=20: return 'repeated_fragment'
    letters=sum(character.isalpha() and not _number(character) for character in compact)
    encoded=text.encode('utf-8')
    if letters>=160 and letters>=len(compact)*.6 and len(encoded)>=512:
        if len(encoded)/max(1,len(zlib.compress(encoded)))>4.5:
            return 'excessive_compression'
    return None
