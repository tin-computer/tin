---
name: blog-index
description: Trace where a site's posts come from, and write one plan, with the exact files, for a blog index that lists every eligible post within two clicks, at the route the founder chose for articles. website.change applies the plan after the founder approves it.
---

A post nobody can reach from the site gets crawled late and read by few. The index is the
page that fixes that for every post at once, including the ones written next month, as long
as it reads the same source the post route reads. This run plans that index, or the fix to
the one there is, in the repository's own framework, as one patch of at most five files.
It reads the repository and changes nothing there: website.change writes the planned files
once the founder approves the plan.

# Where the articles live

1. **The chosen route.** Read `/home/user/state/content/page-routes.json` when present. Its
   `routes.article` (for example `/blog/{slug}`) is where the founder chose to publish
   articles; the index lives at that route's folder (`/blog`). Answer pages
   (`routes.answer_page`) are listed only when they share the folder.
2. **The repository.** Trace the post route in the repository's code: its URL pattern and
   where it reads posts (a Markdown or MDX folder, a content collection, a CMS client or a
   table), with the draft, unpublished and future-date rules it applies. Read
   `wiki/INDEX.md`'s `### Code map` in the project state as a map, not as proof.
3. **Agree or ask.**
   - A chosen route and a post route that serves it: use them.
   - A chosen route the repository does not serve yet: say so, and let `content.deliver` add
     that route with the next article; plan the index only for posts that exist.
   - No chosen route, but the repository has one post route: use its folder, and say in the
     report that the founder can change it with `save_page_route`.
   - No chosen route and no post route: plan no files. Return no-change with the reason
     `no_article_route` and the founder question from PLAN.md, which the coding agent asks
     once: where on the site articles should go, suggesting the folder the site already uses
     for related content (`/blog`, `/guides`, `/learn`, `/resources`) or `/blog/{slug}`, never
     a Tin term.
   A hosted blog (WordPress, Ghost, Webflow) has no repository change to make: return
   no-change with the settings steps instead.
4. If the post source cannot be traced, return no-change with the reason `source_not_found`
   and a short spec of the index; never write a hand-typed list of posts.

# Inventory and reach

5. List eligible posts from the source with its own exclusions: slug, URL, title, published
   and updated dates, description, first paragraph, author, tags, image and source path. A
   future-dated post is `hidden until YYYY-MM-DD`, not missing.
6. Compare eligible posts with the links the current index, sitemap and feed render. Use
   server HTML from an offline build or renderer when the repository can build without
   network or secrets; breadth-first search `<a href>` links from the index and count clicks
   per post. A link built only in client code is not a verified link. Without offline HTML,
   say reach is unknown and explain the static evidence. Live status, canonical and noindex
   stay unknown in this sandbox.
7. Mode: `auto` plans a new index when there is none or it lists no posts, and a fix
   otherwise. `check` plans no files: it compares the source with the index, sitemap and feed,
   names one next fix and returns no-change with the reason `check_only`.

# The planned change

8. One coherent change, at most five non-post files, in this order until the budget is used:
   (a) the index reads the same source as the post route with its exclusions, never a typed
   list; (b) up to 100 posts all listed on the index, above that numbered pages of
   `page_size`, each with its own title, description and self-canonical, never noindex;
   (c) the sitemap lists the index, category pages and every post, each post's `lastmod` from
   its updated or published date, never build time; (d) an RSS 2.0 feed at `/rss.xml` when
   none exists (latest 20 posts, absolute links, canonical-URL GUIDs, RFC 822 dates), keeping
   any existing feed URL and GUIDs; (e) categories only from existing tags, at least three
   posts each and at most eight pages, under the index folder, never the only path to a post;
   (f) cards with the linked title, published date (and `Updated` when different), the
   description or the first paragraph cut at a sentence under 160 characters, and the post's
   own image with width and height, lazy from the second row; (g) newest first, slug as the
   tie-break; featured posts (the `featured` input, or the three with the most 90-day clicks)
   above the list and still in it.
9. Never plan package.json, a lockfile, or build, CI or deploy settings (Tin refuses such a
   plan), never add dependencies, and never alter post URLs, titles, OG images or post files,
   or add ItemList or CollectionPage markup to the index. Add an existing signup form once if
   the site has one; never build a new one. Leave post markup to the audit's fixes and topics
   to `content.plan`.
10. Check in a scratch copy: copy the snapshot outside `/home/user/project`, write the planned
    files there, check them for whitespace errors, run the repository's own tests or build
    when they run offline, and, when a renderer runs, add one synthetic post in the scratch
    copy only and confirm the index and sitemap pick it up without a template edit. Record
    each check not run and why.

# Numbers and the report

11. Measure with MEASUREMENT.md: posts in the source, posts within two clicks, and per post the
    28-day search clicks and impressions against the previous 28 days, with 90-day clicks for
    the featured choice. Unknown stays unknown with its reason, never zero.
12. Write the plan with PLAN.md's headings and its `blog-index-patch.json` block. Each planned
    file's `content` is its full text after the change, exactly what you checked in the
    scratch copy; `action` is `update` for a file the snapshot has and `create` otherwise.
    Open no pull request; never merge, deploy or publish.
