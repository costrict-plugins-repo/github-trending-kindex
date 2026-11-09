"""Independent contract tests; refs S# refer to future-recall contract v1.

Tester authored without implementation inspection. Validator owns execution.
See tests/fixtures/future_self_heldout/TEST_LEDGER.md for falsifiability/gaps.
"""
from __future__ import annotations

import copy
import json
import logging
import math
import sqlite3
import subprocess
import sys

import pytest
from pydantic import BaseModel, ValidationError

from kindex.config import Config, RecallCueConfig
from kindex.store import Store
from kindex.retrieve import hybrid_search
from kindex import recall_cues as cues


@pytest.fixture
def store(tmp_path, monkeypatch):
    # S1/S8: explicit disposable store, no live profiles or provider calls.
    for key in ('KIN_PROFILE', 'KIN_PROJECT', 'KIN_PROJECT_PATH'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    cfg = Config(data_dir=str(tmp_path / 'data'))
    result = Store(cfg)
    yield result
    result.close()


def add(store, node_id='source', **kwargs):
    return store.add_node('Pump vibration investigation',
                          content='The pump did not need replacement. At dusk Nera found the loose mounting bolt.',
                          node_id=node_id, **kwargs)


def sidecar_rows(store):
    return [tuple(r) for r in store.conn.execute('SELECT * FROM recall_cue_sets ORDER BY source_id')]


def sidecar_fts(store):
    return [tuple(r) for r in store.conn.execute('SELECT source_id,text FROM recall_cues_fts ORDER BY source_id')]


def ids(rows):
    return [row[0] for row in rows]


def search(store, query='pump', **kwargs):
    return hybrid_search(store, query, top_k=10, use_vectors=False, expand_graph=False, **kwargs)


def test_default_config_and_strict_bounds():
    """S1: no-op default and bounded opt-in knobs."""
    cfg = RecallCueConfig()
    assert isinstance(cfg, BaseModel)
    assert (cfg.enabled, cfg.candidate_limit, cfg.rank_weight) == (False, 20, .10)
    assert Config().recall_cues == cfg
    for value in (0, 101, True, 1.5, '20'):
        with pytest.raises((ValidationError, ValueError)):
            RecallCueConfig(candidate_limit=value)
    for value in (0, -1, 1.01, float('nan'), float('inf')):
        with pytest.raises((ValidationError, ValueError)):
            RecallCueConfig(rank_weight=value)
    for value in (1, 100):
        assert RecallCueConfig(candidate_limit=value).candidate_limit == value
    assert RecallCueConfig(rank_weight=1).rank_weight == 1


def test_spans_origins_distinctness_and_determinism():
    """S3: source exact spans, qualified statement and independent entry points."""
    node = {'id':'s', 'title':'Pump vibration investigation',
            'content':'The pump did not need replacement. At dusk Nera found the loose mounting bolt.',
            'aka':['night workshop rattle', 'pump shudder']}
    first = cues.generate_cues(node)
    assert first == cues.generate_cues(copy.deepcopy(node))
    assert 1 <= len(first) <= 3
    assert len({cue.text for cue in first}) == len(first)
    assert any(c.origin == 'source_alias' and c.text in node['aka'] for c in first)
    assert any(c.origin == 'generated' for c in first)
    assert any(c.support_field == 'content' for c in first)
    for cue in first:
        assert isinstance(cue, BaseModel)
        assert cue.kind in {'alias','problem','context'}
        assert cue.origin in {'source_alias','generated'}
        assert cue.support_field in {'title','content','aka'}
        source = '\n'.join(node['aka']) if cue.support_field == 'aka' else node[cue.support_field]
        assert 0 <= cue.support_start < cue.support_end <= len(source)
        support = source[cue.support_start:cue.support_end]
        assert support.strip() and support in cue.text
        assert len(cue.text) <= 240
        if cue.origin == 'source_alias':
            assert cue.support_field == 'aka' and cue.text == support
        if 'replacement' in support:
            assert 'did not need replacement' in support


def test_long_and_empty_source_inputs():
    """S3: bounded source inspection and no vacuous support."""
    assert cues.generate_cues({'title':'', 'content':'', 'aka':[]}) == []
    node = {'title':'Short useful title', 'content':'a' * 12000 + ' forbidden_tail_fact', 'aka':[]}
    for cue in cues.generate_cues(node):
        assert 'forbidden_tail_fact' not in cue.text
        if cue.support_field == 'content':
            assert cue.support_end <= 12000
    string_alias = {'title':'Title', 'content':'', 'aka':'literal alias wording'}
    aliases = [c for c in cues.generate_cues(string_alias) if c.origin == 'source_alias']
    assert aliases and aliases[0].text == string_alias['aka']


def test_fingerprint_tracks_authority_but_not_usage():
    """S4: changing facts/provenance/scope invalidates; usage does not."""
    original = {'id':'s', 'title':'Fact', 'content':'Do not replace it', 'aka':['alias'],
                'extra':{'project':'amber', 'nested':{'reason':'certificate'}},
                'audience':'team', 'domains':['workshop'], 'prov_activity':'manual',
                'status':'active', 'created_at':'2026-01-01', 'standing':'present',
                'weight':.7, 'updated_at':'2026-01-02', 'last_accessed':'2026-01-02'}
    baseline = cues.source_fingerprint(original)
    assert baseline == cues.source_fingerprint(dict(reversed(list(original.items()))))
    usage = copy.deepcopy(original)
    usage.update(weight=.1, updated_at='2027-01-01', last_accessed='2027-01-01')
    assert cues.source_fingerprint(usage) == baseline
    for key,value in {'content':'Replace it', 'title':'Other', 'aka':['different'],
                      'audience':'private', 'domains':['other'], 'prov_activity':'import',
                      'status':'archived', 'created_at':'2025-01-01', 'standing':'ratified',
                      'extra':{'project':'violet', 'nested':{'reason':'certificate'}}}.items():
        changed = copy.deepcopy(original)
        changed[key] = value
        assert cues.source_fingerprint(changed) != baseline, key


@pytest.mark.parametrize('limit', [0, -1, 1001, True, 2.5, '2'])
def test_build_rejects_invalid_limits(store, limit):
    """S2: strict bounds reject instead of coercing or truncating."""
    with pytest.raises(ValueError):
        cues.build_cues(store, limit=limit)


def test_bounded_selection_idempotence_and_no_source_writes(store, monkeypatch):
    """S1/S2/S4: cap before generation, ordered ID dedup, metadata-only writes."""
    for node_id in ('c','b','a'):
        add(store, node_id)
    source_before = [tuple(r) for r in store.conn.execute('SELECT * FROM nodes ORDER BY id')]
    original = cues.generate_cues
    seen = []
    def record(node):
        seen.append(node['id'])
        return original(node)
    def prohibited(*args, **kwargs):
        pytest.fail('S2 forbids access-mutating get_node reads')
    monkeypatch.setattr(cues, 'generate_cues', record)
    monkeypatch.setattr(store, 'get_node', prohibited)
    result = cues.build_cues(store, limit=2)
    assert seen == ['a','b']
    assert result['selected'] == result['built'] == 2
    assert all(type(result[key]) is int for key in ('selected','built','unchanged','missing','stale'))
    snapshot = sidecar_rows(store)
    repeated = cues.build_cues(store, limit=2)
    assert repeated['unchanged'] == 2 and repeated['built'] == 0
    assert sidecar_rows(store) == snapshot
    seen.clear()
    explicit = cues.build_cues(store, node_ids=['c','c','missing','a'], limit=3)
    assert explicit['selected'] == 3 and explicit['missing'] == 1
    assert explicit['built'] == 1 and explicit['unchanged'] == 1
    assert [tuple(r) for r in store.conn.execute('SELECT * FROM nodes ORDER BY id')] == source_before
    status = cues.cue_status(store)
    assert status['sets'] == 3 and 3 <= status['cues'] <= 9
    assert len(sidecar_fts(store)) == 3


@pytest.mark.parametrize('field,value', [
    ('content','The pump was replaced.'), ('prov_activity','different origin'),
    ('audience','private'), ('domains',['other']), ('extra',{'project':'other'}),
])
def test_changed_source_is_immediately_unusable(store, field, value):
    """S4/S5: source hydration validates facts and access metadata before use."""
    add(store, audience='team')
    cues.build_cues(store)
    assert ids(cues.cue_candidates(store, 'pump')) == ['source']
    store.update_node('source', **{field:value})
    assert cues.cue_candidates(store, 'pump') == []
    result = cues.build_cues(store)
    assert result['built'] == 1
    assert ids(cues.cue_candidates(store, 'pump')) == ['source']


@pytest.mark.parametrize('action', ['archive','delete','supersede'])
def test_inactive_sources_never_redirect_cue_votes(store, action):
    """S4/S6: old cue cannot rank deleted/archived/superseded source or successor."""
    add(store)
    cues.build_cues(store)
    assert ids(cues.cue_candidates(store, 'pump')) == ['source']
    if action == 'archive':
        store.update_node('source', status='archived')
    elif action == 'delete':
        store.delete_node('source')
    else:
        store.supersede_node('source', 'A wholly different successor about orchids')
    assert cues.cue_candidates(store, 'pump') == []


def test_compare_and_swap_rejects_source_changed_during_generation(store, monkeypatch):
    """S4: race is injected after source read, before metadata publication."""
    add(store)
    generate = cues.generate_cues
    calls = []
    def racing(node):
        calls.append(node['id'])
        value = generate(node)
        store.update_node(node['id'], content='An intervening edit about orchids.')
        return value
    monkeypatch.setattr(cues, 'generate_cues', racing)
    result = cues.build_cues(store)
    assert calls == ['source']
    assert result['stale'] == 1 and result['built'] == 0
    assert sidecar_rows(store) == [] and sidecar_fts(store) == []
    assert store.peek_node('source')['content'] == 'An intervening edit about orchids.'


def test_build_failure_cannot_publish_half_a_sidecar(store):
    """S4/S5: forced SQLite index-write refusal leaves both old records intact."""
    add(store)
    cues.build_cues(store)
    old_sets, old_fts = sidecar_rows(store), sidecar_fts(store)
    store.update_node('source', content='The pump has a newly observed rattle.')
    reached = []
    def authorizer(action, table, column, db, trigger):
        if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE) and (table or '').startswith('recall_cues_fts'):
            reached.append(table)
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    store.conn.set_authorizer(authorizer)
    try:
        with pytest.raises(Exception):
            cues.build_cues(store)
    finally:
        store.conn.set_authorizer(None)
    assert reached
    assert sidecar_rows(store) == old_sets and sidecar_fts(store) == old_fts


def test_unique_scored_candidates_filter_before_limit_and_read_only(store):
    """S5: each source votes once; explicit access predicate is actually reached."""
    for i in range(28):
        add(store, f'n{i:02}', aka=['pump shudder'], audience='private' if i < 26 else 'team')
    cues.build_cues(store)
    before = sidecar_rows(store), sidecar_fts(store)
    seen = []
    def allowed(node):
        seen.append(node['id'])
        return node['audience'] == 'team'
    candidates = cues.cue_candidates(store, 'pump shudder', limit=2, node_filter=allowed)
    assert set(ids(candidates)) == {'n26','n27'}
    assert set(ids(candidates)) <= set(seen)
    assert len(ids(candidates)) == len(set(ids(candidates)))
    assert all(math.isfinite(score) and score > 0 for _,score in candidates)
    assert candidates == cues.cue_candidates(store, 'pump shudder', limit=2, node_filter=allowed)
    assert (sidecar_rows(store), sidecar_fts(store)) == before
    assert cues.cue_candidates(store, '') == []
    assert cues.cue_candidates(store, '" ( ) : *') == []


def test_disabled_parity_and_enabled_source_only_evidence(store):
    """S1/S6: building metadata leaves disabled retrieval unchanged."""
    add(store, aka=['workshop clatter'])
    baseline = search(store)
    assert baseline and baseline[0]['id'] == 'source'
    cues.build_cues(store)
    assert search(store) == baseline
    store.config.recall_cues.enabled = True
    enabled = search(store)
    assert enabled and len({r['id'] for r in enabled}) == len(enabled)
    source = store.peek_node('source')
    for result in enabled:
        assert result['content'] == source['content'] and result['title'] == source['title']
        assert not any(key in result for key in ('cues','cues_json','recall_cues','support_start','support_end'))
    # S6: disabled search must not consult broken optional sidecar storage.
    store.config.recall_cues.enabled = False
    store.conn.execute('DROP TABLE recall_cues_fts')
    store.conn.commit()
    assert search(store) == baseline


def test_explicit_predicate_and_current_source_fences(store):
    """S6: predicate reaches allowed/denied candidates; cues cannot bypass fences."""
    add(store, 'allowed', audience='team')
    add(store, 'denied', audience='private')
    add(store, 'expired', extra={'expires':'2000-01-01'})
    add(store, 'archived')
    store.update_node('archived', status='archived')
    cues.build_cues(store)
    store.config.recall_cues.enabled = True
    seen = []
    def predicate(node):
        seen.append(node['id'])
        return node['audience'] != 'private'
    # Explicit audience on the positive control avoids permissive default traps.
    store.update_node('expired', audience='team')
    result = search(store, node_filter=predicate)
    assert 'allowed' in seen and 'denied' in seen
    assert [r['id'] for r in result] == ['allowed']
    assert search(store, trusted_only=True, node_filter=predicate) == []
    store.verify_node('allowed', verified_by='tester', prov_method='manual-review',
                      verified_at='2026-01-01T00:00:00+00:00')
    cues.build_cues(store, node_ids=['allowed'])
    assert [r['id'] for r in search(store, trusted_only=True, node_filter=predicate)] == ['allowed']


def test_sidecar_failure_falls_back_with_private_warning(store, caplog):
    """S5/S6: real SQL failure, ordinary source results, no private log payload."""
    add(store)
    store.add_node('canary_private_query_847', 'Private Nera fixture detail', node_id='private-log-source')
    baseline = search(store, 'canary_private_query_847')
    assert baseline
    cues.build_cues(store)
    store.conn.execute('DROP TABLE recall_cues_fts')
    store.conn.commit()
    store.config.recall_cues.enabled = True
    with caplog.at_level(logging.WARNING):
        result = search(store, 'canary_private_query_847')
    assert result == baseline
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings
    assert 'canary_private_query_847' not in caplog.text
    assert 'Nera' not in caplog.text and 'loose mounting bolt' not in caplog.text


def test_v15_upgrade_and_required_columns(store):
    """S4: pre-sidecar database reopens forward with durable nodes intact."""
    add(store)
    before = store.peek_node('source')
    store.conn.execute('DROP TABLE IF EXISTS recall_cues_fts')
    store.conn.execute('DROP TABLE IF EXISTS recall_cue_sets')
    store.conn.execute("UPDATE meta SET value='15' WHERE key='schema_version'")
    store.conn.commit()
    config = store.config
    store.close()
    upgraded = Store(config)
    try:
        assert upgraded.get_meta('schema_version') == '16'
        assert upgraded.peek_node('source') == before
        assert cues.build_cues(upgraded)['built'] == 1
        assert cues.cue_status(upgraded)['sets'] == 1
        columns = {r['name'] for r in upgraded.conn.execute('PRAGMA table_info(recall_cue_sets)')}
        assert {'source_id','source_hash','generator_version','generated_at','cues_json'} <= columns
        assert upgraded.schema_drift() == {}
        upgraded.conn.execute('DROP TABLE recall_cue_sets')
        upgraded.conn.execute('CREATE TABLE recall_cue_sets(source_id TEXT PRIMARY KEY)')
        upgraded.conn.commit()
        assert 'recall_cue_sets' in upgraded.schema_drift()
    finally:
        upgraded.close()


def test_cli_and_mcp_share_explicit_bounded_administration(store, monkeypatch):
    """S7: real CLI subprocess and MCP calls operate on the same disposable store."""
    from kindex import mcp_server
    add(store, 'a')
    add(store, 'b')
    def cli(*args):
        return subprocess.run([sys.executable,'-m','kindex.cli','recall-cues',*args,
                               '--data-dir',str(store.config.data_dir),'--json'],
                              text=True, capture_output=True, timeout=30)
    built = cli('build','--node','a','--node','a','--limit','1')
    assert built.returncode == 0, built.stderr
    payload = json.loads(built.stdout)
    assert payload['selected'] == payload['built'] == 1
    monkeypatch.setattr(mcp_server, '_get_store', lambda: (store, store.config))
    monkeypatch.setattr(mcp_server, '_store', store)
    monkeypatch.setattr(mcp_server, '_config', store.config)
    mcp_server.recall_cues_build(node_ids=['b'], limit=1)
    assert cues.cue_status(store)['sets'] == 2
    before = sidecar_rows(store)
    output = mcp_server.recall_cues_status()
    assert output is not None
    status = cli('status')
    assert status.returncode == 0, status.stderr
    assert json.loads(status.stdout) == cues.cue_status(store)
    assert sidecar_rows(store) == before
    assert cli('build','--limit','0').returncode != 0
    assert sidecar_rows(store) == before


@pytest.mark.parametrize('node_ids', ['source', [1], [''], ['source', None], ['x'] * 1001])
def test_malformed_ids_fail_before_work(store, monkeypatch, node_ids):
    """S2 clarification: malformed ID batches rejected before any generation."""
    add(store)
    def forbidden(node):
        pytest.fail('malformed batch reached generation')
    monkeypatch.setattr(cues, 'generate_cues', forbidden)
    with pytest.raises(ValueError):
        cues.build_cues(store, node_ids=node_ids)
    assert sidecar_rows(store) == []


def test_retired_selection_and_missing_index_repair(store):
    """S2 clarification: retired sources skip; unchanged requires intact FTS."""
    add(store, 'active')
    add(store, 'retired')
    store.update_node('retired', status='archived')
    first = cues.build_cues(store)
    assert first['selected'] == first['built'] == 1
    explicit = cues.build_cues(store, node_ids=['retired'])
    assert explicit['selected'] == explicit['skipped'] == 1
    store.conn.execute("DELETE FROM recall_cues_fts WHERE source_id='active'")
    store.conn.commit()
    assert cues.cue_candidates(store, 'pump') == []
    repaired = cues.build_cues(store, node_ids=['active'])
    assert repaired['built'] == 1 and repaired['unchanged'] == 0
    assert ids(cues.cue_candidates(store, 'pump')) == ['active']


def test_fts_indexes_support_not_labels_and_uses_or_tokens(store):
    """S5 clarification: generated labels cannot act as universal recall keys."""
    add(store)
    cues.build_cues(store)
    node = store.peek_node('source')
    support = []
    for cue in cues.generate_cues(node):
        field = node.get(cue.support_field, '')
        if isinstance(field, list):
            field = '\n'.join(field)
        support.append(field[cue.support_start:cue.support_end])
    indexed = sidecar_fts(store)[0][1]
    allowed_words = set(' '.join(support).lower().split())
    assert set(indexed.lower().split()) <= allowed_words
    assert ids(cues.cue_candidates(store, 'pump unrelated_missing_term')) == ['source']
    assert cues.cue_candidates(store, ' '.join(f'absent{i}' for i in range(32)) + ' pump') == []
    assert ids(cues.cue_candidates(store, ('pump ' * 35) + 'unknown')) == ['source']
    assert ids(cues.cue_candidates(store, '"pump" OR (unknown):*')) == ['source']


def test_explicit_scope_filters_neighbor_titles_and_answer_gather(store):
    """S6 clarification: source access bounds apply to returned edges and gather."""
    from kindex import answer
    add(store, 'visible', audience='team')
    store.add_node('Secret neighbor canary', 'pump content', node_id='hidden', audience='private')
    store.add_edge('visible', 'hidden', edge_type='relates_to')
    cues.build_cues(store)
    store.config.recall_cues.enabled = True
    predicate = lambda node: node.get('audience') == 'team'
    result = hybrid_search(store, 'pump', top_k=5, use_vectors=False,
                           expand_graph=True, node_filter=predicate)
    assert [row['id'] for row in result] == ['visible']
    assert 'Secret neighbor canary' not in json.dumps(result)
    token = answer._SCOPE.set(predicate)
    try:
        gathered = answer.gather(store, ['pump'], 5)
        assert [row['id'] for row in gathered] == ['visible']
        assert 'Secret neighbor canary' not in json.dumps(gathered)
    finally:
        answer._SCOPE.reset(token)


@pytest.mark.parametrize('ordinary_contribution', [False, True])
def test_final_hydration_removes_obsolete_cue_contribution(store, monkeypatch, ordinary_contribution):
    """S6 clarification: edit after cue acquisition removes only stale cue votes."""
    from kindex import retrieve
    add(store, audience='team')
    cues.build_cues(store)
    store.config.recall_cues.enabled = True
    real_fts = store.fts_search
    fts_visits = []
    def ordinary_channel(*args, **kwargs):
        rows = real_fts(*args, **kwargs)
        fts_visits.append([row['id'] for row in rows])
        return rows if ordinary_contribution else [row for row in rows if row['id'] != 'source']
    monkeypatch.setattr(store, 'fts_search', ordinary_channel)
    store.config.recall_cues.enabled = False
    ordinary_before = search(store, ranking='rrf')
    store.config.recall_cues.enabled = True
    real_candidates = cues.cue_candidates
    acquired = []
    def race(*args, **kwargs):
        candidates = real_candidates(*args, **kwargs)
        acquired.extend(ids(candidates))
        store.update_node('source', content='The pump investigation is now corrected by a certificate.')
        return candidates
    monkeypatch.setattr(cues, 'cue_candidates', race)
    if hasattr(retrieve, 'cue_candidates'):
        monkeypatch.setattr(retrieve, 'cue_candidates', race)
    actual = search(store, ranking='rrf')
    assert acquired == ['source'] and any('source' in visit for visit in fts_visits)
    if not ordinary_contribution:
        assert actual == []
    else:
        assert [row['id'] for row in actual] == ['source']
        assert actual[0]['content'] == 'The pump investigation is now corrected by a certificate.'
        # Independently retrieved pre-edit source contribution survives unboosted.
        # This differential invariant is not an observed-output golden oracle.
        assert actual[0]['rrf_score'] == ordinary_before[0]['rrf_score']
        assert actual[0]['confidence'] == ordinary_before[0]['confidence']


@pytest.mark.parametrize('rrf_k,weight', [(9,.1),(60,.7)])
def test_rrf_uses_rank_units_and_configured_weight(store, rrf_k, weight):
    """S6 clarification: rank-1 source FTS and rank-1 cue contribute in same units."""
    add(store)
    cues.build_cues(store)
    store.config.recall_cues.enabled = True
    store.config.recall_cues.rank_weight = weight
    store.config.ranking.rrf_k = rrf_k
    result = search(store, ranking='rrf')
    assert [row['id'] for row in result] == ['source']
    assert result[0]['rrf_score'] == round((1+weight)/(rrf_k+1),6)


def test_disabled_never_calls_sidecar_but_enabled_does(store, monkeypatch):
    """S6: an instrumented live public sidecar proves the disabled no-call path."""
    from kindex import retrieve
    add(store)
    cues.build_cues(store)
    original=cues.cue_candidates
    calls=[]
    def instrumented(*args,**kwargs):
        calls.append(True)
        return original(*args,**kwargs)
    monkeypatch.setattr(cues,'cue_candidates',instrumented)
    if hasattr(retrieve,'cue_candidates'):
        monkeypatch.setattr(retrieve,'cue_candidates',instrumented)
    baseline=search(store)
    assert baseline and calls == []
    store.config.recall_cues.enabled=True
    assert search(store)
    assert calls


@pytest.mark.parametrize('content,protected', [
    ('Set the control voltage to 1.5 volts before starting the pump.', '1.5'),
    ('Use status.example.test to check the pump schedule.', 'status.example.test'),
    ('Send the pump inspection to ops@example.test for review.', 'ops@example.test'),
    ('Ask Dr. Vale to inspect the pump before startup.', 'Dr. Vale'),
])
def test_generated_spans_preserve_decimal_address_and_abbreviation_context(content, protected):
    """S3: exact extractive context may not become partial numeric/reference facts."""
    node = {'id':'span-boundary', 'title':'Pump startup instructions', 'content':content, 'aka':[]}
    generated = cues.generate_cues(node)
    spans = [cue for cue in generated if cue.support_field == 'content']
    assert spans, 'S3 prioritizes the useful source task/context entry point'
    start = content.index(protected)
    end = start + len(protected)
    assert any(protected in content[cue.support_start:cue.support_end] for cue in spans)
    for cue in spans:
        # A supported slice that overlaps a number/reference must contain it whole.
        if cue.support_start < end and start < cue.support_end:
            assert cue.support_start <= start and cue.support_end >= end
            assert protected in cue.text


def test_trusted_cue_expiry_uses_explicit_evaluation_clock(store, monkeypatch):
    """S6/S9: ordinary and cue-only trust paths use the same supplied past clock."""
    from datetime import datetime
    add(store, audience='team', extra={'expires':'2020-02-01'})
    store.verify_node('source', verified_by='fixture-reviewer', prov_method='manual-review',
                      verified_at='2019-12-01T00:00:00+00:00')
    cues.build_cues(store)
    timing = {'evaluation_time':'2020-01-15T12:00:00+00:00',
              'recency_time':datetime(2020,1,15,12)}
    ordinary = search(store, trusted_only=True, **timing)
    assert [row['id'] for row in ordinary] == ['source']
    # Positive control: source is a real indexed sidecar candidate before quota/fences.
    assert ids(cues.cue_candidates(store,'pump')) == ['source']
    real_fts = store.fts_search
    reached = []
    def suppress_ordinary(*args, **kwargs):
        rows = real_fts(*args, **kwargs)
        reached.append([row['id'] for row in rows])
        return [row for row in rows if row['id'] != 'source']
    monkeypatch.setattr(store,'fts_search',suppress_ordinary)
    store.config.recall_cues.enabled = True
    cue_only = search(store, trusted_only=True, **timing)
    assert reached
    assert [row['id'] for row in cue_only] == ['source']
    assert search(store, trusted_only=True, evaluation_time='2020-03-01T12:00:00+00:00',
                  recency_time=datetime(2020,3,1,12)) == []


def test_scope_filter_also_bounds_linked_kinbase_question_metadata(store):
    """S6: an allowed fact cannot disclose denied linked-question content/owners."""
    shared = {'repo':'fixture-repo', 'logical_key':'fixture-key', 'mode':'raw'}
    store.add_node('Pump specification', 'The pump runs at the recorded voltage.', node_id='F',
                   node_type='concept', audience='team', extra={'kinbase':shared})
    store.add_node('Restricted owner question', 'PRIVATE_QUESTION_CONTENT_924', node_id='Q',
                   node_type='question', audience='private',
                   extra={'kinbase':{**shared, 'owner_identity':'private-owner',
                                     'owner_role':'reviewer', 'status':'open'}})
    cues.build_cues(store)
    store.config.recall_cues.enabled = True
    unfiltered = search(store, 'pump')
    fact = next(row for row in unfiltered if row['id'] == 'F')
    # Establish real linked-question enrichment before testing its access boundary.
    assert fact.get('kinbase_unknowns')
    linked_before = json.dumps(fact['kinbase_unknowns'])
    assert 'PRIVATE_QUESTION_CONTENT_924' in linked_before or 'private-owner' in linked_before
    seen = []
    def only_fact(node):
        seen.append(node['id'])
        return node['id'] == 'F'
    filtered = search(store, 'pump', node_filter=only_fact)
    assert [row['id'] for row in filtered] == ['F']
    assert 'F' in seen
    # Entire emitted result, not just one possible nested representation, is bounded.
    emitted = json.dumps(filtered)
    for private_value in ('PRIVATE_QUESTION_CONTENT_924', 'private-owner', 'Restricted owner question'):
        assert private_value not in emitted
    linked_after = json.dumps(filtered[0].get('kinbase_unknowns', []))
    assert '"Q"' not in linked_after
