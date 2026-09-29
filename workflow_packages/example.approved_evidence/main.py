"""A source-contract example, deliberately unregistered in the public Registry."""

import hashlib


def run(ctx, inputs):
    posts = ctx["evidence"]["posts"]
    if not posts["present"]:
        raise ValueError("An approved post batch is required")
    content = posts["content"]
    if (
        not content.strip()
        or posts["run_id"] != inputs["posts_run_id"]
        or hashlib.sha256(content.encode("utf-8")).hexdigest() != posts["sha256"]
    ):
        raise ValueError("The selected post batch differs from its source")
    return {
        "path": "reports/SELECTED_POST_BATCH.md",
        "content": (
            "# Selected approved post batch\n\n"
            f"Source run: `{posts['run_id']}`\n\n"
            f"Source file: `{posts['path']}` at `{posts['revision']}`\n\n"
            f"SHA-256: `{posts['sha256']}`\n\n" + content
        ),
    }
