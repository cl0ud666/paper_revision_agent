"""Read one existing checkpoint, replay preview/export into a separate QA folder.

Never resumes the graph, calls a model, or changes the saved task.
"""
import argparse
import json
from pathlib import Path
from unittest.mock import patch

from docx import Document

from app.config import get_settings
from app.document.docx_model import DocxSource, w
from app.document.docx_redline import export_redline
from app.document.final_review import compose_changes
from app.document.preview import prepare_visual_review
from app.workflow.harness import Harness


def main(run_id, output):
    harness = Harness(thread_id='web-' + run_id)
    state = dict(harness.graph.get_state(harness._config()).values)
    folder = get_settings().paths.run_dir(run_id)
    metadata = next((folder / 'preview').glob('*/pages.json'))
    page_data = json.loads(metadata.read_text(encoding='utf-8'))
    before = compose_changes(state)
    output.mkdir(parents=True, exist_ok=True)
    with patch('app.document.preview.render_pages', return_value=(page_data, metadata.parent)):
        result = prepare_visual_review(state, output)
    ready = result['final_patches']
    assert len(ready) == len(before) == 24, result['review_diagnostics']
    assert not result['final_manual_items'], result['final_manual_items']
    original = Path(state['source_docx']).read_bytes()
    exported = export_redline(original, [ready], str(output / 'verified.docx'))
    assert len(exported.applied) == 24 and not exported.skipped
    assert exported.comments == 24
    document = Document(exported.out_path)
    checked = 0
    for item in ready:
        matches = [p for p in document.paragraphs if p.text.strip() == item['before'].strip()]
        assert len(matches) == 1, item['before']
        for run in matches[0].runs:
            if not run.text:
                continue
            assert run.font.size.pt == item['params']['font_size_pt']
            assert run._element.find(f'{w("rPr")}/{w("rFonts")}').get(w('eastAsia')) == item['params']['font_name']
        checked += 1
    assert DocxSource(original).text == DocxSource(Path(exported.out_path).read_bytes()).text
    summary = {'checked_headings': checked, 'written': len(exported.applied), 'comments': exported.comments,
               'pages': sorted({r['page'] for p in ready for r in p['rects']}),
               'placements': [{'before': p['before'], 'rects': p['rects']} for p in ready],
               'saved_task_unchanged': True, 'model_called': False}
    (output / 'verification.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in summary.items() if k != 'placements'}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    main(args.run_id, args.output)
