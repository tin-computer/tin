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
key = os.environ.get("TIN_LITE_LINKEDIN_E2B_API_KEY")
if not key or key == os.environ.get("E2B_API_KEY"):
    parser.error("supply the dedicated LinkedIn compute key, distinct from E2B_API_KEY")
template = Template().from_dockerfile(Path("sandbox/linkedin/Dockerfile").read_text())
Template.build(template, alias=args.alias, cpu_count=1, memory_mb=1024, api_key=key)
