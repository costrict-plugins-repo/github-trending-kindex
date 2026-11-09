#!/usr/bin/env python3
"""S9 independent offline paired evaluation; no answer generation or tuning.

Usage: PYTHONHASHSEED=0 PYTHONPATH=src python scripts/future_recall_ablation.py --work-dir NEW_DIR
Validator executes only after implementation and this runner are frozen.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import socket
import statistics
import sys
import time
from unittest.mock import patch

FIXED_ISO = '2026-10-15T12:00:00+00:00'
FIXED_EPOCH = datetime.fromisoformat(FIXED_ISO).timestamp()
FIXTURE = Path(__file__).resolve().parents[1] / 'tests' / 'fixtures' / 'future_self_heldout'
PROHIBITED_ATTEMPTS = []


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls.fromtimestamp(FIXED_EPOCH, timezone.utc)
        return value.astimezone(tz) if tz else value.replace(tzinfo=None)

    @classmethod
    def utcnow(cls):
        return cls.now(timezone.utc).replace(tzinfo=None)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def check_manifest(path):
    result = {}
    for line in path.read_text().splitlines():
        expected, name = line.split()
        actual = digest(path.parent / name)
        if actual != expected:
            raise ValueError(f'Frozen fixture mismatch: {name}')
        result[name] = actual
    return result


def reject_network(*args, **kwargs):
    PROHIBITED_ATTEMPTS.append('prohibited network/vector/provider call')
    raise RuntimeError('S1/S9/S10 offline evaluation prohibits provider/network calls')


def disk_bytes(directory):
    return sum(p.stat().st_size for p in directory.rglob('*') if p.is_file())


def index_digest(store):
    # Freeze logical sidecar records without displaying generated text.
    sets = [dict(r) for r in store.conn.execute('SELECT * FROM recall_cue_sets ORDER BY source_id')]
    fts = [dict(r) for r in store.conn.execute('SELECT source_id,text FROM recall_cues_fts ORDER BY source_id')]
    canonical = json.dumps({'sets':sets,'fts':fts}, sort_keys=True, separators=(',',':'))
    return hashlib.sha256(canonical.encode()).hexdigest()


def seed(store, sources):
    for source in sources:
        store.add_node(source['title'], content=source['content'], node_id=source['id'],
                       node_type=source['type'], audience=source['audience'],
                       domains=[source['scope']], prov_activity='independent-fictional-fixture',
                       extra={'fixture_scope':source['scope']})
    # Apply normal source lifecycle once before any query. Never per-query.
    for source in sources:
        if source['supersedes']:
            old = store.peek_node(source['supersedes'])
            store.update_node(old['id'], status='superseded',
                              extra={**old['extra'], 'superseded_by':source['id']})
    store.conn.execute('UPDATE nodes SET created_at=?, updated_at=?, last_accessed=?',
                       (FIXED_ISO,FIXED_ISO,FIXED_ISO))
    store.conn.commit()


def summarize(rows):
    answerable = [r for r in rows if r['answerable']]
    unanswerable = [r for r in rows if not r['answerable']]
    latencies = sorted(r['latency_ms'] for r in rows)
    def fraction(items, key):
        return sum(bool(r[key]) for r in items)/len(items) if items else None
    return {'trials':len(rows), 'answerable_trials':len(answerable),
            'no_answer_trials':len(unanswerable),
            'recall':fraction(answerable, 'hit'),
            'mrr':sum(r['reciprocal_rank'] for r in answerable)/len(answerable) if answerable else None,
            'answerable_candidate_rate':fraction(answerable, 'has_candidates'),
            'no_answer_candidate_rate':fraction(unanswerable, 'has_candidates'),
            'latency_mean_ms':statistics.mean(latencies) if latencies else None,
            'latency_median_ms':statistics.median(latencies) if latencies else None,
            'latency_p95_ms':latencies[max(0,math.ceil(.95*len(latencies))-1)] if latencies else None}


def report_markdown(report):
    lines = ['# Offline synthetic future-recall ablation', '',
             'Synthetic, same-author feasibility only. No answer generation, real-user claims, or temporal generalization.', '',
             f"Fixed evaluation clock: `{FIXED_ISO}`. Provider requests: 0; provider cost: $0.", '',
             'The 32-note corpus and 72 main trials were frozen before implementation output. Wrong-hint trials are a separate fixed stress set.', '',
             '| Set / condition | k | Arm | Answerable | Recall | MRR | No-answer candidate rate | Median ms |',
             '|---|---:|---|---:|---:|---:|---:|---:|']
    def fmt(value):
        return 'n/a' if value is None else f'{value:.4f}'
    for section, groups in report['summary'].items():
        for group in groups:
            value=group['metrics']
            lines.append(f"| {section} / {group['condition']} | {group['top_k']} | {group['arm']} | {value['answerable_trials']} | {fmt(value['recall'])} | {fmt(value['mrr'])} | {fmt(value['no_answer_candidate_rate'])} | {fmt(value['latency_median_ms'])} |")
    lines += ['', 'Recall treats authored duplicate relevant IDs as one answer-equivalence group: retrieving either supports a hit. MRR is truncated at the requested k. Candidate rate measures whether retrieval returned anything, not answer accuracy or hallucination.', '',
              'Separate top-k=5 and top-k=10 calls preserve the ordinary candidate-window behavior at each budget. Both arms use the same source graph, explicit access predicate, vector-off setting and no graph expansion. The cue arm adds bounded sidecar candidates and storage; total index cost is not equal. Any observed effect may come from this extra OR lexical candidate path and index budget; it cannot be attributed specifically to future-self reasoning.', '',
              'Corrections use normal superseded-source fences. Historical queries remain unchanged; their inaccessible relevant sources are reported as corpus/policy mismatches rather than relabeled.', '',
              '## First relevant rank changes', '', 'These comparisons describe first relevant source rank only. Full returned-source order may change even when this rank is unchanged; inspect the per-trial source ID lists separately.', '', '| Set / condition | k | Inquiry | Baseline rank | Cue rank | Outcome |', '|---|---:|---|---:|---:|---|']
    for row in report['comparisons']:
        if row['change'] != 'unchanged':
            lines.append(f"| {row['section']} / {row['condition']} | {row['top_k']} | {row['query_id']} | {row['baseline_rank']} | {row['cue_rank']} | {row['change']} |")
    lines += ['', '## Frozen evidence', '']
    for name,value in report['freeze'].items():
        lines.append(f'- {name}: `{value}`')
    lines += ['', '## Storage and limits', '', '```json', json.dumps(report['storage'],indent=2), '```', '',
              'Main and stress per-trial query IDs, returned source IDs, relevant ranks, scores and timings are in the JSON report. Source/query wording is retained only in the separately frozen fixtures.', '',
              '## Limitations and possible follow-up', '',
              'Small fictional notes and same-session authored queries understate long-delay ambiguity. Hints are useful remembered context in the main set; wrong hints use fixed unrelated hint rotation and are only a stress probe. No public benchmark labels or generated-query oracles are used. Cold/warm cache and arm-order effects can affect latency; run order alternates arms per query but this is not a latency benchmark.', '',
              'A separate provider-backed study would need explicit approval and current model pricing confirmation before any call. See the final integration report for the proposed source, data, token and cost caps. Keep independent delayed queries and compare equal index budgets. This runner implements no provider work.', '']
    return '\n'.join(lines)


def run(args):
    if os.environ.get('PYTHONHASHSEED') != '0':
        raise ValueError('Launch with PYTHONHASHSEED=0; runtime assignment cannot fix set iteration')
    # Verify bytes before opening any Kindex store. Queries are parsed only after index freeze.
    fixture_hashes = check_manifest(FIXTURE/'FREEZE.sha256')
    stress_hashes = check_manifest(FIXTURE/'STRESS_FREEZE.sha256')
    sources = json.loads((FIXTURE/'sources.json').read_text())['sources']
    args.work_dir.mkdir(parents=True, exist_ok=False)
    # Never inspect or resolve user config/profiles; force disposable paths before imports.
    for key in ('ANTHROPIC_API_KEY','OPENAI_API_KEY','GEMINI_API_KEY','GOOGLE_API_KEY',
                'VOYAGE_API_KEY','COHERE_API_KEY','KIN_PROFILE','KIN_PROJECT','KIN_PROJECT_PATH'):
        os.environ.pop(key,None)
    os.environ['XDG_STATE_HOME']=str(args.work_dir/'state')
    os.environ['KIN_NO_SCHEDULER_WRITES']='1'
    os.chdir(args.work_dir)
    with ExitStack() as guard:
        guard.enter_context(patch.object(socket.socket,'connect',reject_network))
        guard.enter_context(patch.object(socket.socket,'connect_ex',reject_network))
        guard.enter_context(patch('socket.create_connection',reject_network))
        guard.enter_context(patch('socket.getaddrinfo',reject_network))
        guard.enter_context(patch('time.time',return_value=FIXED_EPOCH))
        from kindex.config import Config
        from kindex.store import Store
        from kindex import recall_cues, retrieve, vectors
        blocked = [vectors.vector_search, vectors.embed_text, vectors.embed_document_chunks]
        # Deny known vector entry points and any already imported aliases.
        for name,module in list(sys.modules.items()):
            if name.startswith('kindex.'):
                for attribute,value in list(vars(module).items()):
                    if any(value is original for original in blocked):
                        guard.enter_context(patch.object(module,attribute,reject_network))
        # Patch loaded Kindex datetime imports, avoiding provider/source-code inspection.
        frozen_modules=[]
        for name,module in list(sys.modules.items()):
            if name.startswith('kindex.') and getattr(module,'datetime',None) is datetime:
                guard.enter_context(patch.object(module,'datetime',FrozenDatetime))
                frozen_modules.append(name)
        stores={}
        storage={}
        for arm in ('baseline','cues'):
            directory=args.work_dir/arm
            store=Store(Config(data_dir=str(directory)))
            guard.callback(store.close)
            seed(store,sources)
            store.config.recall_cues.enabled = arm == 'cues'
            stores[arm]=store
            store.conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            storage[arm]={'before_build_bytes':disk_bytes(directory)}
        build_start=time.perf_counter()
        build=recall_cues.build_cues(stores['cues'],limit=100)
        build_ms=(time.perf_counter()-build_start)*1000
        for arm,store in stores.items():
            store.conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            storage[arm]['after_build_bytes']=disk_bytes(args.work_dir/arm)
        # This durable freeze exists before loading inquiry text or executing queries.
        freeze={**fixture_hashes, **stress_hashes,
                'generator_module_sha256':digest(recall_cues.__file__),
                'runner_sha256':digest(__file__),
                'cue_index_before_query_sha256':index_digest(stores['cues'])}
        (args.work_dir/'PRE_QUERY_FREEZE.json').write_text(json.dumps(freeze,indent=2)+'\n')
        main=json.loads((FIXTURE/'queries.json').read_text())['queries']
        stress=json.loads((FIXTURE/'wrong_hint_stress.json').read_text())['queries']
        trials=[]
        for section,queries in (('main',main),('wrong_hint_stress',stress)):
            for number,query in enumerate(queries):
                wording=query['query'] + ('\nRemembered context: '+query['hint'] if query['hint'] else '')
                # The actual access scope never comes from remembered words.
                def allowed(node, query=query):
                    return ((node.get('extra') or {}).get('fixture_scope') == query['scope']
                            and node.get('audience') in query['allowed_audiences'])
                for top_k in (5,10):
                    for arm in (('baseline','cues') if number%2 == 0 else ('cues','baseline')):
                        before=time.perf_counter()
                        rows=retrieve.hybrid_search(stores[arm],wording,top_k=top_k,
                                                    use_vectors=False,expand_graph=False,node_filter=allowed,
                                                    recency_time=FrozenDatetime.now(),evaluation_time=FIXED_ISO)
                        elapsed=(time.perf_counter()-before)*1000
                        returned=[row['id'] for row in rows]
                        if len(returned) != len(set(returned)):
                            raise AssertionError('S6 duplicate source results')
                        if not all(allowed(row) for row in rows):
                            raise AssertionError('S6 access predicate bypass')
                        for row in rows:
                            source=stores[arm].peek_node(row['id'])
                            if row.get('content') != source.get('content'):
                                raise AssertionError('S6 evidence changed from source')
                        relevant=query['expected_relevant_source_ids']
                        ranks=[i+1 for i,node_id in enumerate(returned) if node_id in relevant]
                        first=min(ranks) if ranks else None
                        retired=[node_id for node_id in relevant
                                 if (stores[arm].peek_node(node_id) or {}).get('status') in ('archived','superseded')]
                        trials.append({'section':section,'query_id':query['id'],'pair_id':query['pair_id'],
                                       'condition':query['condition'],'category':query['category'],
                                       'top_k':top_k,'arm':arm,'answerable':query['answerable'],
                                       'returned_source_ids':returned,'expected_relevant_source_ids':relevant,
                                       'relevant_ranks':ranks,'first_relevant_rank':first,
                                       'hit':first is not None,'reciprocal_rank':1/first if first else 0,
                                       'has_candidates':bool(rows),'policy_fenced_relevant_ids':retired,
                                       'latency_ms':elapsed,
                                       'scores':[{'id':r['id'],'confidence':r.get('confidence'),
                                                  'rrf_score':r.get('rrf_score')} for r in rows]})
        if index_digest(stores['cues']) != freeze['cue_index_before_query_sha256']:
            raise AssertionError('S5 query evaluation mutated sidecar records')
        summary={}
        for section in ('main','wrong_hint_stress'):
            summary[section]=[]
            conditions=('without_hints','with_hints') if section=='main' else ('wrong_hint',)
            for condition in conditions:
                for top_k in (5,10):
                    for arm in ('baseline','cues'):
                        rows=[r for r in trials if (r['section'],r['condition'],r['top_k'],r['arm']) == (section,condition,top_k,arm)]
                        summary[section].append({'condition':condition,'top_k':top_k,'arm':arm,'metrics':summarize(rows)})
        paired={}
        for row in trials:
            paired.setdefault((row['section'],row['query_id'],row['top_k']),{})[row['arm']]=row
        comparisons=[]
        for (section,query_id,top_k),arms in paired.items():
            baseline,cue=arms['baseline'],arms['cues']
            delta=cue['reciprocal_rank']-baseline['reciprocal_rank']
            comparisons.append({'section':section,'query_id':query_id,'condition':cue['condition'],
                                'top_k':top_k,'baseline_rank':baseline['first_relevant_rank'],
                                'cue_rank':cue['first_relevant_rank'],
                                'change':'improved' if delta>0 else 'regressed' if delta<0 else 'unchanged'})
        if PROHIBITED_ATTEMPTS:
            raise AssertionError('Offline guard caught a prohibited call, even if an optional path swallowed its exception')
        report={'python_hash_seed':os.environ['PYTHONHASHSEED'],'contract':'future-recall v1 S1-S10','evaluation_clock':FIXED_ISO,
                'frozen_datetime_modules':frozen_modules,'freeze':freeze,'storage':storage,
                'build':build,'build_latency_ms':build_ms,'summary':summary,
                'comparisons':comparisons,'trials':trials,
                'provider_calls':0,'provider_cost_usd':0,'use_vectors':False,'expand_graph':False,
                'cue_candidate_limit':stores['cues'].config.recall_cues.candidate_limit,
                'cue_rank_weight':stores['cues'].config.recall_cues.rank_weight,
                'ordinary_candidate_windows':'Unchanged at each separately requested top_k; extra sidecar channel has its own budget.',
                'claim_limit':'Synthetic extractive feasibility; no answer correctness, temporal or human memory effectiveness claim.'}
        (args.work_dir/'report.json').write_text(json.dumps(report,indent=2,allow_nan=False)+'\n')
        (args.work_dir/'report.md').write_text(report_markdown(report))
        print(args.work_dir/'report.json')
        print(args.work_dir/'report.md')


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-dir',required=True,type=lambda p:Path(p).resolve(),
                        help='New disposable directory; existing paths are refused')
    run(parser.parse_args())
