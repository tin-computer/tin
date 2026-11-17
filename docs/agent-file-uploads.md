# Agent file uploads

A member's coding agent makes files on the member's machine that belong in project Files:
LinkedIn card images, a video, a logo SVG. MCP carries no file bytes, and a text commit
(`commit_project_changes`) can't hold an image or copy a long SVG exactly. So the agent asks
for an upload link and sends each file to it with curl, as films do
([feature films](feature-films.md)).

1. **`prepare_file_upload(project_id, folder)`** (MCP) checks that the member is on the project
   and that `folder` is an ordinary place in Files, then answers with a link, the folder, when
   the link expires and the commands to use it:

   ```bash
   curl --fail-with-body -T p1.png '<url>'
   curl --fail-with-body -T '{p1.png,p2.png,p3.png}' '<url>'
   ```

   `curl -T` appends the local file's name to a link that ends in a slash, so `p1.png` lands at
   `<folder>/p1.png`. A name may include subfolders (`art/logo.svg`).
2. **`PUT /api/file-uploads/<token>/<name>`** keeps one file. The link is the credential: random,
   kept only as a SHA-256 hash (`file_upload_links`, migration 084), tied to one project and one
   folder, good for 45 minutes and at most 200 files. Each file is committed on its own, at the
   current head, as the member who asked for the link (`client_id` `upload-link`); a file
   already at that path is replaced. If another write lands first, the upload commits on the
   new head, up to three tries.
3. **What Tin keeps.** PNG and JPEG (5 MB) and MP4 (64 MB) are checked exactly as a dashboard
   upload is (`project_media.validate_media`): complete files, real dimensions. Any other name
   must be UTF-8 text of at most 1 MB and passes the same checks as a text commit: no
   protected paths, no credentials. Anything else is refused with what to fix, and nothing is
   written.

A link stops working when it expires, when it has taken 200 files, when the member leaves the
project or when the project is deleted (deletion removes its links). Files served back from
Files keep the [untrusted file delivery](project-file-security.md) rules; an uploaded SVG is
previewed as an image, never as a document.
