# -*- coding: utf-8 -*-
"""A fixed set of cases detection must get right, and a runner for them.

There was no repeatable way to ask "is detection any good?". Individual checks
were run by hand and the answers were not written down, so a curator's edit could
improve one community and silently break another with nothing to notice it.

Every case here is drawn from the focus group transcript, the survey, or from a
false positive the vocabulary would produce -- never invented to be easy. Three
outcomes, because the system genuinely has three:

  ``flag``     the deterministic tier must fire. A miss here is hate speech going
               unrecorded, which is the failure the project exists to prevent.

  ``quiet``    it must not fire. These are the ordinary sentences that share
               words with the lexicon: piety, a nationality, a recipe, an
               affectionate kinship term. A hit here is a false positive, and
               false positives are what get a monitoring project dismissed.

  ``review``   it must fire *and* carry exemption hints. Documentation, research
               and survivor testimony quote the words being documented. Matching
               is a substring test and cannot tell a report from a taunt, so the
               honest contract is that the cheap tier flags it and passes on what
               a reader needs to clear it. A finding here with no hints is a bug:
               it means nobody downstream was told this could be legitimate.
"""

from __future__ import annotations

from . import match as match_mod

# ---------------------------------------------------------------------------
# Parent posts. What a comment sits under decides what it means.
# ---------------------------------------------------------------------------

ACCIDENT = 'حادث سير على طريق دهوك زاخو'
RESTAURANT = 'افتتاح مطعم جديد في اربيل'
SINJAR = 'اخبار من سنجار عن الايزيدية بعد سنوات من الابادة'
YAZIDI_FEAST = 'الايزيديين يحتفلون بعيد الجماعية في لالش'
ASSYRIAN = 'الاشوريين في سهل نينوى يطالبون باعادة الاعمار في بغديدا'
ETHIOPIA = 'مباراة ودية بين المنتخب العراقي واثيوبيا'
HR_REPORT = 'تقرير حقوقي عن الناجيات الايزيديات من سنجار'
CHURCH_HISTORY = 'محاضرة عن تاريخ كنيسة المشرق والسريان في العراق'


CASES = [
    # -- the minimal pair the whole design turns on ------------------------
    {
        'id': 'refuge-formula-under-an-accident',
        'text': 'اعوذ بالله من الشيطان الرجيم',
        'parent': ACCIDENT,
        'expect': 'quiet',
        'why': 'Said daily by millions. Under a road accident it is ordinary piety '
               'and nothing else. If this flags, the system is unusable in Iraq.',
    },
    {
        'id': 'refuge-formula-under-a-yazidi-post',
        'text': 'اعوذ بالله من الشيطان الرجيم',
        'parent': SINJAR,
        'expect': 'flag',
        'why': 'The same words aimed at the community are the devil-worship taunt. '
               'Transcript rows 2, 5, 6. Only the parent post separates the two.',
    },

    # -- explicit attacks, which must fire wherever they appear ------------
    {
        'id': 'devil-worship-libel',
        'text': 'اليزيديون يعبدون الشيطان',
        'parent': SINJAR,
        'expect': 'flag',
        'why': 'The single most reported slur in the field data.',
    },
    {
        'id': 'incitement-to-kill',
        'text': 'قتل اليزيديين حلال',
        'parent': SINJAR,
        'expect': 'flag',
        'why': 'Transcript row 15. Explicit religious sanction for killing.',
    },
    {
        'id': 'expulsion-call',
        'text': 'رجعو لمناطقكم مكانكم مو هنا',
        'parent': YAZIDI_FEAST,
        'expect': 'flag',
        'why': 'Transcript row 16.',
    },
    {
        'id': 'survivor-incitement',
        'text': 'اذا بنية يزيدية ناجية ليش مدا تقتلوها',
        'parent': HR_REPORT,
        'expect': 'flag',
        'why': 'Transcript rows 5, 36. The highest-severity pattern in the data, '
               'and it was unreachable until this trope was given surface forms.',
    },
    {
        'id': 'holiday-discrimination',
        'text': 'ليش يعطون لليزيديين عطلة لصيامهم',
        'parent': YAZIDI_FEAST,
        'expect': 'flag',
        'why': 'Transcript rows 4, 13. Also previously unreachable.',
    },

    # -- Assyrian, which had no working gate at all before -----------------
    {
        'id': 'assyrian-indigeneity-denial',
        'text': 'الاشورين دخلاء من قبل البريطانيين',
        'parent': ASSYRIAN,
        'expect': 'flag',
        'why': 'Survey respondents 3, 19, 23, 29. Assyrians were the most targeted '
               'community in the survey and had no topic markers, so every '
               'context-dependent Assyrian term was dead.',
    },
    {
        'id': 'assyrian-time-bomb',
        'text': 'المسيحيين قنبلة موقوتة',
        'parent': ASSYRIAN,
        'expect': 'flag',
        'why': 'Survey. Rights claims reframed as a demographic threat.',
    },

    # -- the false positives the vocabulary would produce ------------------
    {
        'id': 'ethiopians-in-a-football-post',
        'text': 'اللاعبين الاثيوبيين كانوا سريعين',
        'parent': ETHIOPIA,
        'expect': 'quiet',
        'why': 'A nationality. It is in the lexicon because the survey recorded it '
               'used to deny Assyrian indigeneity -- as an explicit term it flagged '
               'every mention of Ethiopia.',
    },
    {
        'id': 'stinking-about-food',
        'text': 'الاكل هنا گەنی ما اكلته',
        'parent': RESTAURANT,
        'expect': 'quiet',
        'why': 'Ordinary Kurdish for "stinking". The curator kept the bare word for '
               '"dirty" out of the lexicon for exactly this reason.',
    },
    {
        'id': 'kiriv-as-kinship',
        'text': 'كريڤو مبروك عليك المولود',
        'parent': RESTAURANT,
        'expect': 'quiet',
        'why': 'Kiriv is the godparent bond, used affectionately every day between '
               'families. A taunt only when aimed.',
    },
    {
        'id': 'nestorian-in-church-history',
        'text': 'الكنيسة النسطورية لها تاريخ طويل في العراق',
        'parent': CHURCH_HISTORY,
        'expect': 'review',
        'why': 'A real denominational term. The Assyrian topic gate is open here '
               'because the lecture is about Assyrians, so the cheap tier fires and '
               'must hand on the historical and academic exemptions.',
    },
    {
        'id': 'swimming-off-topic',
        'text': 'اطفالنا لا يسبحون في النهر بالشتاء',
        'parent': ACCIDENT,
        'expect': 'quiet',
        'why': 'The verb is both "to bathe" and "to glorify God". The purity libel '
               'it came from is real; this sentence is about winter.',
    },

    # -- documentation, which quotes what it documents ---------------------
    {
        'id': 'captives-in-a-rights-report',
        'text': 'وثق التقرير شهادات عن السبايا في 2014',
        'parent': HR_REPORT,
        'expect': 'review',
        'why': 'The word for the captives of 2014 is in every human rights report '
               'and every survivor account. Matching cannot tell documentation from '
               'a taunt, so it must fire and say what would clear it.',
    },
    {
        'id': 'counter-speech-naming-the-libel',
        'text': 'من يقول ان الايزيديين عبدة الشيطان فهو جاهل بدينهم',
        'parent': SINJAR,
        'expect': 'review',
        'why': 'Refuting the libel requires repeating it. This is the most common '
               'false positive in any lexicon system, and the exemption hints are '
               'how a reviewer sees it in one glance.',
    },
]


def run(knowledge, case_id=None) -> dict:
    """Run every case through the real matcher. Returns a report.

    `case_id` is passed through to `evaluate` so case-scoped vocabulary can be
    exercised; the gold set itself is all general vocabulary.
    """
    results = []
    for case in CASES:
        result = match_mod.evaluate(
            {'text': case['text'], 'parent_post_text': case['parent']},
            knowledge,
            case_id=case_id,
        )
        expect = case['expect']
        if expect == 'quiet':
            ok = not result.matched
        elif expect == 'flag':
            ok = result.matched
        else:                                   # review
            ok = result.matched and bool(result.exemption_hints)

        results.append({
            'id': case['id'],
            'expect': expect,
            'matched': result.matched,
            'hints': list(result.exemption_hints),
            'explain': result.explain(),
            'ok': ok,
            'why': case['why'],
        })

    passed = sum(1 for r in results if r['ok'])
    return {
        'results': results,
        'passed': passed,
        'total': len(results),
        'failures': [r for r in results if not r['ok']],
    }


def describe(report) -> str:
    """The report as something worth reading in a terminal."""
    lines = [f"gold set: {report['passed']}/{report['total']} correct"]
    if report['failures']:
        lines.append('')
        for item in report['failures']:
            got = 'flagged' if item['matched'] else 'quiet'
            if item['expect'] == 'review' and item['matched']:
                got = 'flagged with no exemption hints'
            lines.append(f"  {item['id']}: expected {item['expect']}, got {got}")
            lines.append(f"      {item['explain']}")
            lines.append(f"      {item['why']}")
    return '\n'.join(lines)
