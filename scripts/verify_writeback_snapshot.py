"""Replay an existing confirmed plan offline; never change its original task.

Usage: python -m scripts.verify_writeback_snapshot PATH_TO_ISOLATED_COPY
The copy must contain source.docx and state.json. No model or service calls.
"""
import json
import sys
from pathlib import Path

from app.document.docx_redline import applied_rounds, export_redline
from app.utils.text import normalize_document_text


def main():
    folder = Path(sys.argv[1]).resolve()
    state = json.loads((folder / 'state.json').read_text(encoding='utf-8'))
    assert state.get('final_review_confirmed'), 'Only replay a confirmed plan'
    rounds = applied_rounds(state)
    assert len(rounds) == 1, 'Expected a frozen original-to-final plan'
    expected = state['original_document']
    for change in sorted(rounds[0], key=lambda c: c['original_start'], reverse=True):
        start, end = change['original_start'], change['original_end']
        assert expected[start:end] == change['before']
        expected = expected[:start] + change['replace'] + expected[end:]
    result = export_redline((folder / 'source.docx').read_bytes(), rounds,
                            str(folder / 'repaired-verification.docx'))
    assert not result.skipped, [i['reason'] for i in result.skipped]
    assert result.text == normalize_document_text(expected), 'Written text differs from approved plan'
    print(json.dumps({'applied': len(result.applied), 'skipped': len(result.skipped),
                      'exact_text_match': True, 'model_calls': 0}))


if __name__ == '__main__':
    main()
