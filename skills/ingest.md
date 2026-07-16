---
name: ingest
description: 把资料收进知识库（先看类目 → 提取 → 入库 → 报告收录明细）
requires: [file_extract, rag_list_categories, rag_upsert_category, rag_insert_document]
---

You are filing material into the user's knowledge base (RAG). Whatever goes in will
be retrieved months later stripped of today's context — file it so that future
retrieval makes sense on its own.

## Sequence

1. **`rag_list_categories` first, always.** Reuse an existing category when one
   fits; the knowledge base rots when every ingest invents a new近义 category.
   Create via `rag_upsert_category` only when nothing fits, with a description that
   says what belongs inside — that description is what the auto-classifier sees.
2. **Extract before inserting.** For files, `file_extract` and *read the result*:
   check the text is complete and not mojibake/OCR 乱码. Garbage in the knowledge
   base is worse than nothing — it surfaces in future answers wearing the authority
   of a stored document.
3. **`rag_insert_document` per document** with a title that identifies the source
   (原文件名或来源 + 主题). Pass the category you chose in step 1.
4. **Report the manifest**: for each document — title, category, and one line on
   what it contains. The user must be able to audit what just became "knowledge".

## Rules

1. **Never insert the same document twice.** If unsure whether something is already
   in, ask or search first (`rag_search` if available) — duplicates skew every
   future retrieval score toward the duplicated content.
2. **Do not summarize away the source.** Insert the extracted text, not your summary
   of it — retrieval needs the original wording; summaries are lossy at exactly the
   details future questions will ask about.
3. **Chunk sanity over volume**: if extraction produced fragments (broken tables,
   header noise), clean the obvious noise but do not rewrite content.
4. **A failed insert is reported, not retried blind.** If the RAG service rejects a
   document, state which one failed and why; partial success must be visible.
