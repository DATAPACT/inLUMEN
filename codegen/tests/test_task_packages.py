import json
import pytest
from pydantic import ValidationError
from app.generator import files_from_payload
from app.schemas import GenerateNodeScriptRequest
from app.task_package_contract import validate_package, task_data_contract


def test_generated_task_contains_public_portable_manifest():
    request=GenerateNodeScriptRequest.model_validate({'context':{'target_node':{'flow_id':'task','type':'task','label':'Task'},'expected_outputs':[{'name':'result','filename':'result.json','kind':'json','format':'json'}]}})
    files=files_from_payload(request,{'main_py':'import json, os\nfrom pathlib import Path\nPath(os.environ["PIPELINE_OUTPUT_DIR"], "result.json").write_text("{}")','requirements':[]})
    portable={file.filename:file.content for file in files if file.filename!='node-manifest.json'}
    package=validate_package(portable)
    assert package['metadata']['version']==1
    assert package['data_contract']['outputs'][0]['filename']=='result.json'
    assert task_data_contract(json.loads(portable['inlumen.task.json']))==package['data_contract']


def test_multiple_outputs_require_an_explicit_bundle():
    with pytest.raises(ValidationError):
        GenerateNodeScriptRequest.model_validate({'context':{'target_node':{'flow_id':'task','type':'task','label':'Task'},'expected_outputs':[{'name':name,'filename':name+'.json','kind':'json'} for name in ['one','two']]}})


def test_sentiment_prefers_canonical_anonymized_transcript_over_original_alias():
    import ast
    from app.trusted_adapters import roberta_sentiment_function_source
    from app.model_plans import ROBERTA_SENTIMENT_PLAN
    node={'function_name':'sentiment','outputs':[{'name':'sentiment','filename':'sentiment.json','kind':'json','format':'json'}],'implementation_plan':ROBERTA_SENTIMENT_PLAN}
    tree=ast.parse(roberta_sentiment_function_source(node))
    extractor=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='extract_text')
    namespace={}
    exec(compile(ast.Module(body=[extractor],type_ignores=[]),'<sentiment-text-selector>','exec'),namespace)
    assert namespace['extract_text']({'text':'ORIGINAL SECRET','transcript':'[REDACTED] happy'})=='[REDACTED] happy'
    assert namespace['extract_text']({'text':'external plain text'})=='external plain text'

    assert namespace['extract_text']({'text':'ORIGINAL SECRET','transcript':''})==''
