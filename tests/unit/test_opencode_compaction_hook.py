"""The native compaction hook changes summary setup, preserving all history."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

PLUGIN = Path(__file__).resolve().parents[2] / "tools" / "opencode-pac-compaction" / "index.js"


@pytest.mark.parametrize("provider", ["pac", "other"])
def test_compaction_hook_preserves_transcript_model_options_and_does_not_supply_summary(provider):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the installed native OpenCode plugin")
    script = "import plugin, {COMPACTION_SYSTEM} from " + json.dumps(PLUGIN.as_uri()) + ";\n" + """
    let callback;
    const cleanup = await plugin.setup({session:{hook:async(kind,handler,scope)=>{
      if(kind!=='compaction'||scope.providerID!=='pac')throw Error('wrong registration');
      callback=handler;return{dispose:()=>{}};
    }}});
    const messages=[{role:'assistant',content:[{type:'tool-call',id:'call_a',name:'read',input:{path:'file'}}]},
      {role:'tool',content:[{type:'tool-result',id:'call_a',name:'read',result:{type:'content',value:[{type:'text',text:'actual result'}]}}]}];
    const tools={read:{description:'full original',input:{type:'object'}}};
    const event={model:{providerID:PROVIDER,id:'pac-local'},messages,tools,system:[{text:'original harness'}],options:{maxTokens:1024}};
    const before=JSON.stringify(event);
    await callback(event);
    process.stdout.write(JSON.stringify({sameMessages:event.messages===messages,originalToolsIntact:tools.read.description==='full original',
      noResult:!('result'in event),options:event.options,model:event.model,
      changed:before!==JSON.stringify(event),system:event.system,toolNames:Object.keys(event.tools)}));
    await cleanup();
    """.replace("PROVIDER", json.dumps(provider))
    result = subprocess.run([node, "--input-type=module", "-e", script],
                            capture_output=True, text=True, encoding="utf-8", check=True)
    data = json.loads(result.stdout)
    assert data["sameMessages"] and data["originalToolsIntact"] and data["noResult"]
    assert data["options"] == {"maxTokens": 1024}
    assert data["model"] == {"providerID": provider, "id": "pac-local"}
    assert data["changed"] == (provider == "pac")
    assert data["toolNames"] == ([] if provider == "pac" else ["read"])
