from copy import deepcopy

from app.agents.prompts import TERM_POLICY
from app.agents.revision import _scope_terms
from app.document.terminology import normalize_terminology_decisions
from app.workflow.interaction import prepare_response
from app.workflow.nodes import normalize_issue_payload


def issue(id, problem, options):
    return {'id': id, 'kind': 'decision', 'problem': problem, 'options': options,
            'category': '语言表达', 'severity': 'medium', 'span_label': 'P01',
            'evidence': 'SimCLR 依赖当前批次内的负样本。'}


def sample():
    return [issue(3, '同一段落内 batch 相关术语中英文混用且不统一，批次与批量含义不同',
                  ['首次出现写批大小（Batch Size），后续统一用批大小', '首次出现写批大小（Batch Size），后续统一用Batch Size']),
            issue(4, '批次大小与任务要求的术语规范不一致，全文未统一为批大小',
                  ['保留批次大小为32，首次写批大小（Batch Size），后续统一用批大小', '全文统一使用批次大小'])]


def test_same_concept_one_choice_with_no_fake_requirement_or_contradiction():
    document = 'SimCLR 依赖当前批次内的负样本，无需增大当前批量。\n\n预训练批次大小为32。\n'
    findings = sample(); original = deepcopy(findings)
    result = normalize_terminology_decisions(findings, document)
    assert len(result) == 1
    item = result[0]
    assert item['merged_issue_ids'] == [3, 4]
    assert item['terminology_concept'] == 'batch_size'
    assert len(item['span_labels']) == 2
    assert '任务要求' not in item['problem']
    assert all('保留批次大小为32' not in o for o in item['options'])
    assert any('全文统一使用「批次大小」' in o for o in item['options'])
    assert any('全文统一使用「Batch Size」' in o for o in item['options'])
    assert 'batch' not in _scope_terms(item)
    assert '批次' not in _scope_terms(item)
    assert findings == original
    # Repeated normalize is deterministic, so choice indexes cannot drift.
    assert normalize_terminology_decisions(result, document) == result


def test_consistent_translation_and_different_concepts_are_not_errors():
    document = '当前批次内的负样本来自过往批次。\n\n批次大小为32。\n'
    assert normalize_terminology_decisions(sample(), document) == []


def test_first_bilingual_definition_is_not_language_mixing():
    document = '批次大小（Batch Size）设为32。\n\n后续批次大小保持32。\n'
    assert normalize_terminology_decisions(sample(), document) == []


def test_different_concepts_have_separate_choices():
    findings = [sample()[1], issue(7, 'epoch 与训练轮次写法不统一', ['统一使用 Epoch', '统一使用训练轮次'])]
    result = normalize_terminology_decisions(findings, 'batch size 为32，批次大小为32。\n\nepoch 为100，训练轮次为100。\n')
    assert [i['terminology_concept'] for i in result] == ['batch_size', 'epoch']
    assert all('批次大小' not in o for o in result[1]['options'])


def test_generic_similarity_dedupe_never_merges_different_concepts():
    from app.agents.audit import _merge_issues, _merge_systematic
    first = issue(3, '全文术语写法不统一', ['统一使用 Batch Size', '统一使用批次大小'])
    second = issue(4, '全文术语写法不统一', ['统一使用 Epoch', '统一使用训练轮次'])
    assert len(_merge_issues([first, second])) == 2
    assert len(_merge_systematic([first, second], {'merged_systematic': 0})) == 2


def test_real_semantic_dispute_and_explicit_author_rule_are_preserved():
    semantic = issue(2, '误将 epoch 译为批次，混淆了含义', ['修正含义', '核对训练设置'])
    assert normalize_terminology_decisions([semantic], 'epoch 为100。') == [semantic]
    rules = [{'kind': 'rule', 'text': '全文统一为批大小', 'status': 'active'}]
    assert normalize_terminology_decisions([sample()[1]], '批次大小为32。', rules) == [sample()[1]]


def test_author_selection_is_propagated_exactly_to_revision():
    result = normalize_terminology_decisions(sample(), 'batch size为32，批次大小为32。\n')
    chosen = next(i for i, option in enumerate(result[0]['options']) if '全文统一使用「批次大小」' in option)
    request = {'type': 'issue_review', 'issues': result}
    response = prepare_response(request, {'choices': {str(result[0]['id']): chosen}})
    state = normalize_issue_payload(response, result)
    assert state['audit_findings'][0]['suggestion'] == result[0]['options'][chosen]
    assert not state['manual_items']


def test_prompt_does_not_claim_one_translation_is_required():
    assert '不是用户任务要求' in TERM_POLICY
    assert '不制定唯一中文译名' in TERM_POLICY
    assert '不要按段落重复询问' in TERM_POLICY
