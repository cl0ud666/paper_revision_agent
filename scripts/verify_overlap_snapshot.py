"""Read-only replay of a stored first revision round; never resumes the graph.

Run inside the application's configured environment. No LLM, uploads, task
updates, checkpoint writes or result-file replacements are performed.
"""
import argparse
import json

from app.document.patches import Patch, apply_patches
from app.document.final_review import compose_changes
from app.document.spans import build_spans
from app.agents.revision import _scope_targets, _group_targets, _related_spans
from app.config import get_settings
from app.workflow.harness import Harness


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-id', required=True)
    args = parser.parse_args()
    harness = Harness(thread_id=f'web-{args.run_id}', logger=lambda _: None)
    state = harness.graph.get_state(harness._config()).values
    original = state['original_document']
    row = next(r for r in state['revision_history'] if r.get('stage') == 'apply')
    patches = [Patch.from_json(p) for p in row.get('applied', []) + row.get('failed_edits', [])]
    for patch in patches:
        patch.status, patch.conflict_code, patch.conflict_reason = 'accepted', '', ''
    spans = build_spans(original)
    # All offsets must match the immutable source before replay is allowed.
    for patch in patches:
        span = next(s for s in spans if s.id == patch.span_id)
        assert span.text[patch.start_in_span:patch.end_in_span] == patch.before
    outcome = apply_patches(original, spans, patches)
    assert not outcome.skipped, [p.conflict_code for p in outcome.skipped]
    combined = compose_changes({'original_document': original, 'current_document': outcome.document,
                                'applied_patches': [p.to_json() for p in outcome.applied]})
    source_ids = {i for p in combined for i in p['source_patch_ids']}
    assert all(p.patch_id in source_ids for p in patches)
    if state.get('document_version') == 2:
        assert outcome.document == state['current_document'], 'Replay changed the previously applied result'
    related_issues = [i for i in state.get('audit_findings', [])
                      if '摘要' in i.get('problem', '') and '6-way' in i.get('problem', '')]
    for issue in related_issues:
        targets = _scope_targets(spans, [issue])
        assert not targets[0].get('scope_terms'), 'Numeric consistency misrouted as terminology'
        groups = _group_targets(spans, targets, get_settings().context.revision_max_tokens)
        assert len(groups) == 1
        required = {s.id for s in _related_spans(spans, issue)}
        assert required <= {s.id for s in groups[0][0]}
    print(json.dumps({'input_patches': len(patches), 'merged_patches': len(outcome.applied),
                      'conflicts': len(outcome.skipped), 'final_changes': len(combined),
                      'cross_section_issues_verified': len(related_issues),
                      'saved_task_changed': False}, ensure_ascii=False))


if __name__ == '__main__':
    main()
