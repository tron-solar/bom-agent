# create_bom → draft BOM → review-comment pipeline

This is orchestrated by the Railway `bom-trigger` service. The Coperniq MCP is PULL + a few WRITES;
it CANNOT watch for `create_bom` opening — that trigger is the Railway service's job.

## Verified MCP capabilities (as of this build)
- get_project(project_id, include_virtual_properties=True)        [read]
- list_project_files(project_id)  / get_project_file(file_id,…)   [read]  (paginate ?page=N&page_size=100)
- list_project_forms / get_form                                   [read]
- create_project_file(project_id, url, name?, isArchived?, phaseInstanceId?)   [write] — fetches a URL; NO raw upload, NO draft flag
- create_project_comment(project_id, body)                        [write] — plain string body
- update_project_comment(comment_id, project_id, body)            [write]

## TWO UNKNOWNS to confirm before production
1. FILE HOST: create_project_file needs a fetchable URL. The BOM must be uploaded to a bucket first
   (project files already live on coperniq-databank.s3…wasabisys.com). Need the bucket/credentials
   the Railway service writes to.
2. COMMENT @-MENTION FORMAT: create_project_comment takes a plain body. The exact markup that turns
   text into a notifying @-tag (e.g. "@[Name](user:4679)") is NOT verified here. Confirm against
   Coperniq API docs. Until confirmed, post the assignee's NAME in plain text + flag it.

## Stages (Railway orchestrator)

0. TRIGGER (Railway, not MCP): detect create_bom task -> ASSIGNED/open on a project. Capture
   project_id and create_bom assignee id (e.g. Meyer: custom.create_bom_assignee.id = 4679).
1. PULL + CONFIRM PLANSET: get_project; list_project_files (paginated) -> match
   "<First> <Last> REV<L>.pdf", pick highest revision (planset_confirm.py). Stop if none.
   list_project_forms -> "Master Note" -> get_form (mandatory).
2. DOWNLOAD PLANSET: fetch the file's downloadUrl to local disk.
3. RUN ENGINE: extractor -> resolve_racking / electrical blocks -> bom_writer.write_bom().
   If ANY block returns a HARD flag -> DO NOT upload; go to 5b (post holds for a human).
4. HOST + ATTACH: upload xlsx to bucket -> public URL -> create_project_file(name=…_DRAFT.xlsx).
5a. COMMENT + TAG (success): create_project_comment with summary + all NOTE flags + assignee tag.
5b. COMMENT (held): create_project_comment listing the HARD flags; no file attached.

## Reference implementation
See pipeline_create_bom.py — the orchestrator skeleton with every MCP call stubbed as an injected
callable so it runs headless and is unit-testable. Fill `upload_to_bucket` and `mention()` once the
two unknowns are confirmed.
