"""Operator-invoked dedicated build. Does not read or reuse ordinary Tin aliases."""

import argparse
import os
from pathlib import Path

from e2b import Template

parser = argparse.ArgumentParser()
parser.add_argument("--alias", required=True)
args = parser.parse_args()
if not args.alias.startswith("tin-linkedin-"):
    parser.error("use a new tin-linkedin-* alias; existing workflow aliases are refused")
key = os.environ.get("TIN_LITE_LINKEDIN_E2B_API_KEY") or os.environ.get("E2B_API_KEY")
if not key:
    parser.error("supply E2B_API_KEY (or the optional TIN_LITE_LINKEDIN_E2B_API_KEY override)")
root = Path(__file__).resolve().parents[2]
template = Template(file_context_path=root).from_dockerfile(
    (root / "sandbox/linkedin/Dockerfile").read_text()
)
build = Template.build(
    template,
    alias=args.alias,
    cpu_count=1,
    memory_mb=1024,
    api_key=key,
    on_build_logs=lambda entry: print(entry.message, flush=True),
)
print(f"Built {args.alias}: template={build.template_id} build={build.build_id}")
