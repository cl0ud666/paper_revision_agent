from unittest.mock import patch
import pytest

from app.document.citations import lookup_candidate, resolve_citation, citation_consistency_audit


def record(**overrides):
    return {'title':['Momentum Contrast for Unsupervised Visual Representation Learning'],
            'author':[{'given':'Kaiming','family':'He'}], 'published':{'date-parts':[[2020,6]]},
            'DOI':'10.1109/CVPR42600.2020.00975', **overrides}


def test_model_candidate_verified_by_independent_metadata():
    lookup_candidate.cache_clear()
    item = {'action':'insert_citation','params':{'metadata':{'title':record()['title'][0], 'author':'Kaiming He','year':'2020'},
                                              'source_verification':{'verification':'model says verified'}}}
    with patch('app.document.citations._request',return_value={'items':[record()]}):
        resolve_citation(item,'原文没有文献资料')
    assert item['params']['metadata']['doi'] == record()['DOI']
    assert item['params']['source_verification']['source_url'].startswith('https://doi.org/')
    assert '全文论据' in item['params']['source_verification']['verification']


@pytest.mark.parametrize('candidate', [record(title=['Different title']), record(author=[{'family':'Other'}]), record(published={'date-parts':[[2021]]})])
def test_wrong_title_author_or_year_does_not_pass(candidate):
    lookup_candidate.cache_clear()
    with patch('app.document.citations._request',return_value={'items':[candidate]}):
        with pytest.raises(ValueError,match='未唯一匹配'):
            lookup_candidate(record()['title'][0], 'Kaiming He','2020')


def test_ambiguous_match_is_not_first_result_wins():
    lookup_candidate.cache_clear()
    with patch('app.document.citations._request',return_value={'items':[record(),record(DOI='10.1234/other')]}):
        with pytest.raises(ValueError): lookup_candidate(record()['title'][0],'Kaiming He','2020')


def test_same_surname_wrong_given_name_does_not_pass():
    lookup_candidate.cache_clear()
    with patch('app.document.citations._request',return_value={'items':[record()]}):
        with pytest.raises(ValueError): lookup_candidate(record()['title'][0],'Wrong He','2020')


def test_untrusted_url_cannot_be_used_as_doi():
    lookup_candidate.cache_clear()
    with patch('app.document.citations._request') as request:
        with pytest.raises(ValueError): lookup_candidate('Title','Author','2020','http://127.0.0.1:8080/admin')
    request.assert_not_called()


def test_numeric_consistency_finds_missing_unused_duplicate_and_order():
    doc = '# 正文\n\n参考研究[2]提出方法。随后引用[1]和[9]。\n\n# 参考文献\n\n[1] 第一篇文献\n\n[2] 第二篇文献\n\n[2] 重复编号文献\n\n[3] 未引用的文献\n'
    issues = citation_consistency_audit(doc)
    assert any('[9]' in i['problem'] and '没有对应条目' in i['problem'] for i in issues)
    assert any('[3]' in i['problem'] and '未发现' in i['problem'] for i in issues)
    assert any('重复' in i['problem'] for i in issues)
    assert any('不递增' in i['problem'] for i in issues)
    assert all(i['evidence'] in doc for i in issues)


def test_author_year_style_is_not_flagged_as_missing_numeric_citations():
    assert not citation_consistency_audit('# 正文\n\nHe et al. (2020) 提出方法。\n\n# 参考文献\n\nHe et al. Momentum Contrast. 2020.\n')
