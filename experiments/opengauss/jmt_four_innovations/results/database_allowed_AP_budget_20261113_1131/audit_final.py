from pathlib import Path
from datetime import datetime
import hashlib,json,subprocess,sys,time,unittest
from jmt.common.db import Gsql
from jmt.common.provenance import validate_path_hash_evidence
root=Path('.').resolve();run=Path(__file__).resolve().parent
sys.path.insert(0,str(root))
read=lambda p:json.loads(Path(p).read_text())
sha=lambda p:hashlib.sha256(Path(p).read_bytes()).hexdigest()
summary=read(run/'AP_MEMORY_POLICY_RESULT.json');b=read(run/'memory_final/MEMORY_BUDGET.json');count=0
for name in ['database_configured','native_protector_diagnostic']:
 f=read(run/(name+'_frozen_frontier_final')/'SB_SPILL_FRONTIER.json');count+=validate_path_hash_evidence(f['source_artifacts'])
 for p,h in f['model_code_sha256'].items():assert sha(root/p)==h,p
 a=read(run/('AUDIT_'+name+'_UNPRUNED_FINAL.json'));assert a['all_points_exact_match'] and a['points_checked']==55
 assert read(run/('REPLAY_'+name+'_FINAL.log'))['exact_match']
for p,h in b['model_code_sha256'].items():assert sha(root/p)==h,p
assert validate_path_hash_evidence(summary)==7
for name in ['CURRENT_AP_MEMORY_POLICY.json','CURRENT_MEMORY_BUDGET.json','CURRENT_SB_SPILL_FRONTIER.json','CURRENT_INNOVATION4_EVALUATION.json','CURRENT_FIVE_STAGE_RECOMMENDATIONS.json']:
 p=read(root/name);assert sha(root/p['result'])==p['result_sha256'],name
c=Gsql('/opt/openGauss/bin/gsql',run_as_user='omm',timeout_s=15)
s=c.json('h5_tpch',"""SELECT json_build_object('start',pg_postmaster_start_time()::text,
 'target_buffers',(SELECT setting::bigint FROM pg_settings WHERE name='shared_buffers_target'),
 'startup_buffers',(SELECT setting::bigint FROM pg_settings WHERE name='shared_buffers'),
 'max_process_memory_kib',(SELECT setting::bigint FROM pg_settings WHERE name='max_process_memory'),
 'effective_memory_protect',(SELECT setting FROM pg_settings WHERE name='enable_memory_limit'),
 'disable_memory_protect',(SELECT setting FROM pg_settings WHERE name='disable_memory_protect'),
 'other_workload_sessions',(SELECT count(*) FROM pg_stat_activity WHERE datname IN ('h5_tpcc','h5_tpch') AND pid<>pg_backend_pid()))""")
assert s['start']==b['postmaster_identity']['start'] and s['target_buffers']==s['startup_buffers']==1048576
assert s['max_process_memory_kib']==20971520 and s['effective_memory_protect']=='on' and s['disable_memory_protect']=='off' and s['other_workload_sessions']==0,s
assert sha('/opt/openGauss/bin/gaussdb')=='79f7d79af465d7bb4c62f5f3e9fa0992ab7f585a37201846424d2468cf707a7b'
t=time.perf_counter()
with (run/'ALL_TESTS_PROMOTED.log').open('x') as stream:
 result=unittest.TextTestRunner(stream=stream,verbosity=2).run(unittest.defaultTestLoader.discover(str(root/'tests')))
foreign={n:str(m.__file__) for n,m in sys.modules.items() if (n=='jmt' or n.startswith('jmt.')) and getattr(m,'__file__',None) and not str(Path(m.__file__).resolve()).startswith(str(root/'src/jmt')+'/')}
assert result.wasSuccessful() and result.testsRun==711 and not foreign
regression_seconds=time.perf_counter()-t
replay=subprocess.run(['python3','-S','tools/replay_innovation4_evaluation.py','--evaluation','results/innovation4_config_evaluation_20261112_2344/performance_v4/CONFIGURATION_EVALUATION.json','--inputs','results/innovation4_config_evaluation_20261112_2344/performance_v4/FROZEN_REPLAY_INPUTS.json'],capture_output=True,text=True,check=True)
(run/'LEGACY_STEP4_FROZEN_REPLAY.log').write_text(replay.stdout)
changed=['src/jmt/common/database_memory_domain.py','src/jmt/innovation_3_search/memory_budget.py','tools/search_sb_spill_budgets.py','tools/collect_machine_memory_budget.py','tools/predict_operator_spill_curves.py','tools/probe_database_work_mem_domain.py','tools/replay_sb_spill_budgets.py','src/jmt/innovation_4_performance/config_evaluation.py','tools/evaluate_spill_frontier_performance.py','tools/replay_innovation4_evaluation.py','tests/test_config_evaluation.py','tests/test_database_allowed_AP_budget.py']
for name in changed:
 p=run/'source_final'/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes((root/name).read_bytes())
paths={line.split('  ',1)[1] for line in (root/'CODE_SHA256SUMS').read_text().splitlines() if '  ' in line}
paths.update(str(p.relative_to(root)) for p in (root/'src').rglob('*.py'))
paths.update(str(p.relative_to(root)) for p in (root/'tools').glob('*.py'))
paths.update(str(p.relative_to(root)) for p in (root/'tests').glob('test_*.py'))
paths.update(p.name for p in root.glob('*.md'));paths.update(p.name for p in root.glob('CURRENT_*.json'))
(root/'CODE_SHA256SUMS').write_text(''.join(sha(root/p)+'  '+p+'\n' for p in sorted(paths)))
a={'schema':'jmt.database-allowed-AP-budget-final-audit/v1','created_at':datetime.now().astimezone().isoformat(),'source_artifacts_verified_across_frontiers':count,'summary_artifact_hashes_verified':7,'v2_default_is_database_configured':b['default_planning_scenario']=='database_configured','primary_native_remainder_cap_removed':not summary['fixed_AP_six_GiB_or_native_remainder_cap_used_in_primary_planning'],'accepted_actual_SET_values_mib':summary['actually_accepted_session_SET_work_mem_mib'],'two_independent_unpruned_audits_passed_points':110,'normal_expired_statistics_rejected':summary['normal_expired_AP_planning_rejected'],'new_five_stage_prediction_or_optimum_claimed':False,'frozen_budget_sensitivity_only':True,'native_protection_not_bypassed':True,'native_quota_coordination_still_required_for_primary_points':55,'current_database_state':s,'model_and_budget_sources_bound_and_unchanged_during_final_preparation':True,'regression':{'tests_run':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'seconds':regression_seconds,'all_imports_from_candidate':not foreign},'legacy_step4_frozen_replay_matches':json.loads(replay.stdout)['numeric_and_allocation_match'],'changed_source_files':changed,'code_manifest_files':len(paths),'code_manifest_sha256':sha(root/'CODE_SHA256SUMS'),'configuration_applied_or_postmaster_restarted':False,'AP_query_or_large_WM_allocation_executed':False,'report':'AP_MEMORY_POLICY_CURRENT.md'}
(run/'FINAL_AUDIT.json').write_text(json.dumps(a,ensure_ascii=False,indent=2,allow_nan=False)+'\n');print(json.dumps(a,ensure_ascii=False,indent=2))
