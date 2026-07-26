from __future__ import annotations
import json
import uuid
from pathlib import Path
import pytest

from src.catalog.freeze import assert_catalog_frozen,freeze_catalog
from src.catalog.task import build_task_envelope,write_task_envelope
from src.file_fingerprint import compute_sha256
from src.ingest.commit import commit_paper_raw
from src.ingest.commit_recovery import reconcile_commits
from src.ingest.formalization import write_formalization_plan
from src.ingest.workspace import PaperRawWorkspace
from src.catalog_folders.reader import read_category_members
from src.metadata.freeze import assert_metadata_frozen,freeze_metadata
from src.metadata.pdf_identity import extract_pdf_identity_evidence
from src.metadata.pdf_match import build_match_receipt,write_match_receipt
from src.library.paper_number_ledger import PaperNumberLedger
from src.metadata.schema import empty_metadata
from src.utils.atomic_io import atomic_write_json
from src.ingest.stage_manifest import write_stage_manifest
from src.ingest.import_status import write_import_status
from src.workspace.receipt import build_receipt_payload, write_or_validate_discovery_receipt

NUMBER="0000000000000001"

def _metadata(folder: Path)->dict:
    value=empty_metadata(NUMBER); value.pop("metadata_match",None); value["title"]["original"]="Terrain Wind Downscaling"; value["authors"]=[{"full_name":"Jane Smith","family":"Smith","given":"Jane","orcid":"","affiliation":""}]; value["first_author"]={"family":"Smith","display":"Jane Smith"}; value["year"]=2024; value["container"]["journal"]="Journal"; value["identifiers"]["doi"]="10.1234/example"; value["source"].update({"kind":"network_search","provider":"fixture","raw_record_path":"source_records/metadata_source.fixture.json"}); return value

def _catalog(task: dict)->dict:
    candidate=task["source_abstract_candidates"][0]; hashes=task["input_hashes"]
    ref={"asset":"markdown","locator_type":"section","locator":"Results","quote_hint":"improves RMSE","figure_label":None,"image_ref":None}
    return {"schema_version":"3.2","paper_number":NUMBER,"paper_name":task["paper_name_prefix"]+"复杂地形风速降尺度","content_identity":{"content_title_zh":"复杂地形风速降尺度","research_domains":["复杂地形气象"],"document_language":"en"},"abstract":{"source":{"status":"present","origin":candidate["origin"],"language":candidate["language"],"text":candidate["text"],"source_ref":candidate["source_ref"]},"summary_zh":"本文使用状态感知模型研究复杂地形中的风速降尺度，并在观测测试集上比较预测误差。","summary_generation_basis":"abstract_and_full_text","one_sentence_zh":"状态感知模型提高了复杂地形风速降尺度精度。"},"research_context":{"background_zh":"复杂地形造成局地风场差异。","knowledge_gap_zh":"传统模型未区分地形状态。","research_question_zh":"状态感知模型能否提高降尺度精度？","objectives_zh":["比较预测误差"]},"methods":{"overview_zh":"训练状态感知回归并与线性基线比较。","method_types":["统计建模"],"models_or_algorithms":["状态感知回归"],"experimental_design_zh":"时间留出验证","evaluation_metrics":["RMSE"],"comparison_baselines":["线性回归"]},"data_and_study_design":{"data_sources":["地面观测"],"study_region_or_objects_zh":"山地区域","time_range":"2020–2022","spatial_or_temporal_resolution":"逐小时","sample_or_case_description_zh":"多个站点"},"key_findings":[{"finding_zh":"模型降低了测试误差。","importance_zh":"证明状态表示有效。","evidence_refs":[ref]}],"mechanisms":[],"limitations":[],"figures_and_tables":[],"terminology":{"items":[{"term_en":"downscaling","term_zh":"降尺度","definition_zh":"由粗分辨率推断局地状态。"}],"not_applicable_reason":None},"writing_value":{"use_cases":["支持方法比较"],"claims_supported":["状态表示改善预测"],"suitable_sections":["方法"],"comparison_value_zh":"可与基线对比。","cautions_zh":[]},"screening":{"read_decision":"pending","priority":None,"reason_zh":None},"provenance":{"metadata_sha256":hashes["metadata_sha256"],"metadata_freeze_sha256":hashes["metadata_freeze_sha256"],"markdown_sha256":hashes["markdown_sha256"],"conversion_manifest_sha256":hashes["conversion_manifest_sha256"],"catalog_task_sha256":compute_sha256(Path(task["_path"])),"image_hashes":hashes["image_hashes"],"source_record_hashes":hashes["source_record_hashes"],"skill_version":task["skill_version"],"generated_at":"2026-01-01T00:00:00+08:00"}}

def _workspace(tmp_path: Path)->tuple[PaperRawWorkspace,Path,Path,Path]:
    raw=tmp_path/"paper_raw"; papers=tmp_path/"papers"; catalog_dir=tmp_path/"catalog"; ledger_path=catalog_dir/"paper_number_ledger.json"; folder=raw/NUMBER; folder.mkdir(parents=True); ledger=PaperNumberLedger(ledger_path); ledger.reserve_specific_for_paper_raw(NUMBER,folder)
    metadata=_metadata(folder); atomic_write_json(folder/f"{NUMBER}.metadata.json",metadata,indent=2); (folder/f"{NUMBER}.pdf").write_bytes(b"%PDF-1.7\nDOI 10.1234/example\n"); (folder/"source_records").mkdir(); atomic_write_json(folder/metadata["source"]["raw_record_path"],{"title":"Terrain Wind Downscaling","abstract":"Provider abstract"},indent=2)
    evidence=extract_pdf_identity_evidence(pdf_path=folder/f"{NUMBER}.pdf"); write_match_receipt(folder,build_match_receipt(folder,NUMBER,metadata,evidence,provider_records=[metadata["source"]["raw_record_path"]])); freeze_metadata(folder,NUMBER); assert_metadata_frozen(folder,NUMBER)
    markdown="# Terrain Wind Downscaling\n\n## Abstract\nSource abstract text.\n\n## Results\nThe model improves RMSE.\n\n## Discussion\nLimitations.\n"; (folder/f"{NUMBER}.md").write_text(markdown,encoding="utf-8"); (folder/"images").mkdir(); atomic_write_json(folder/f"{NUMBER}.conversion.json",{"pdf_sha256":compute_sha256(folder/f"{NUMBER}.pdf"),"markdown_sha256":compute_sha256(folder/f"{NUMBER}.md")},indent=2)
    # Write stage manifest and .import_status.json so workspace is complete for metadata_staged.
    write_stage_manifest(folder,paper_number=NUMBER,paper_raw_id=NUMBER,workflow_path="network_metadata",source_type="network_search",pdf_source=None,staged_pdf=None)
    write_import_status(folder,"staged_metadata",extra={"paper_number":NUMBER,"paper_raw_id":NUMBER,"source_type":"network_search","source_provider":"fixture","doi":"10.1234/example"})
    # Write a discovery receipt to satisfy lifecycle inspection.
    write_or_validate_discovery_receipt(folder/f"{NUMBER}.discovery_receipt.json",build_receipt_payload(paper_number=NUMBER,candidate_id="test",page_id="test",keyword_id="test",provider="fixture",normalized_doi="10.1234/example"),workspace_root=raw)
    # Promote ledger state to metadata_staged.
    ledger.mark_metadata_staged(NUMBER,folder)
    assert_metadata_frozen(folder,NUMBER); task_path=write_task_envelope(folder,NUMBER); task=json.loads(task_path.read_text(encoding="utf-8")); task["_path"]=str(task_path); catalog=_catalog(task); atomic_write_json(folder/f"{NUMBER}.catalog.json",catalog,indent=2); freeze_catalog(folder,NUMBER,papers_dir=papers,paper_raw_root=raw); assert_catalog_frozen(folder,NUMBER,papers_dir=papers,paper_raw_root=raw)
    return PaperRawWorkspace.from_path(folder),papers,ledger_path,catalog_dir

def test_network_style_frozen_catalog_formalize_commit(tmp_path: Path):
    workspace,papers,ledger_path,catalog_root=_workspace(tmp_path); metadata_hash=compute_sha256(workspace.metadata); catalog_hash=compute_sha256(workspace.catalog); before=set(p.name for p in workspace.root.iterdir()); plan=write_formalization_plan(workspace,papers_dir=papers)
    after_formalize = set(p.name for p in workspace.root.iterdir())
    diff = after_formalize - before
    assert f"{NUMBER}.formalization.json" in diff
    # .import_status.json may already exist from metadata_staged setup; formalization
    # only writes it if absent.
    result=commit_paper_raw(workspace,paper_raw_root=tmp_path/"paper_raw",papers_dir=papers,ledger_path=ledger_path,catalog_root=catalog_root,transactions_dir=tmp_path/"transactions")
    final=papers/result["paper_name"]; assert result["phase"]=="complete" and final.is_dir() and not workspace.root.exists(); assert compute_sha256(final/f"{result['paper_name']}.metadata.json")==metadata_hash; assert compute_sha256(final/f"{result['paper_name']}.catalog.json")==catalog_hash
    assert read_category_members(catalog_root/"all",papers_dir=papers)[0]["paper_number"]==NUMBER
    journals=list((tmp_path/"transactions"/"commit"/"completed").glob("*.json")); assert len(journals)==1 and json.loads(journals[0].read_text(encoding="utf-8"))["phase"]=="complete"

@pytest.mark.parametrize("fault_phase",["prepared","staging_complete","final_installed","ledger_active","category_reconcile_requested","source_deleted"])
def test_commit_recovers_forward_from_durable_phase(tmp_path: Path,fault_phase: str):
    workspace,papers,ledger_path,catalog_root=_workspace(tmp_path); raw=workspace.root.parent; write_formalization_plan(workspace,papers_dir=papers)
    def fail(phase):
        if phase==fault_phase: raise RuntimeError("injected crash")
    with pytest.raises(RuntimeError,match="injected crash"):
        commit_paper_raw(workspace,paper_raw_root=tmp_path/"paper_raw",papers_dir=papers,ledger_path=ledger_path,catalog_root=catalog_root,transactions_dir=tmp_path/"transactions",fault_injector=fail)
    result=reconcile_commits(transactions_dir=tmp_path/"transactions",paper_raw_root=raw,papers_dir=papers,ledger_path=ledger_path,catalog_root=catalog_root)
    assert result and result[0]["phase"]=="complete"
    assert read_category_members(catalog_root/"all",papers_dir=papers)[0]["paper_number"]==NUMBER


def test_commit_reconcile_rejects_ambiguous_active_journals_before_mutation(tmp_path: Path):
    workspace, papers, ledger_path, catalog_root = _workspace(tmp_path)
    transaction_root = tmp_path / "transactions"
    write_formalization_plan(workspace, papers_dir=papers)

    def fail_after_prepare(phase: str) -> None:
        if phase == "prepared":
            raise RuntimeError("injected crash")

    with pytest.raises(RuntimeError, match="injected crash"):
        commit_paper_raw(
            workspace,
            paper_raw_root=tmp_path / "paper_raw",
            papers_dir=papers,
            ledger_path=ledger_path,
            catalog_root=catalog_root,
            transactions_dir=transaction_root,
            fault_injector=fail_after_prepare,
        )

    first_path = next((transaction_root / "commit").glob("*.json"))
    duplicate = json.loads(first_path.read_text(encoding="utf-8"))
    second_id = str(uuid.uuid4())
    duplicate["transaction_id"] = second_id
    duplicate["staging_path"] = str(
        (papers / f".{duplicate['paper_name']}.staging_{second_id}").resolve()
    )
    second_path = transaction_root / "commit" / f"{second_id}.json"
    second_path.write_text(json.dumps(duplicate), encoding="utf-8")

    with pytest.raises(RuntimeError, match="ambiguous_active_transaction"):
        reconcile_commits(
            transactions_dir=transaction_root,
            paper_raw_root=tmp_path / "paper_raw",
            papers_dir=papers,
            ledger_path=ledger_path,
            catalog_root=catalog_root,
            apply=True,
        )

    assert workspace.root.is_dir()
    assert not (papers / duplicate["paper_name"]).exists()
    active_journals = list((transaction_root / "commit").glob("*.json"))
    assert len(active_journals) == 2
    assert {
        json.loads(path.read_text(encoding="utf-8"))["phase"]
        for path in active_journals
    } == {"prepared"}
