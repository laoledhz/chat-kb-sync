# Configure and run

Use a current Python 3.10+ and the absolute path to `scripts/chat_kb.py` for a scheduler. Python and these scripts must be readable from the scheduling account. All config paths below are private and absolute; use a work directory **outside Git and outside any source folder**. For Windows JSON, escape backslashes or use forward slashes.

Save a private config outside the repository (adapt the example paths):

```json
{
  "work_dir": "/absolute/private/chat-kb-runtime",
  "min_idle_seconds": 300,
  "publish_unclassified": false,
  "sources": [
    {
      "type": "export",
      "namespace": "fictional-example",
      "root": "/absolute/private/approved-exports",
      "allowlist": "/absolute/private/allowed-chats.json"
    }
  ],
  "ragflow": {
    "origin": "http://127.0.0.1:9380",
    "dataset_id": "clipboard-value-from-local-ragflow-setup"
  }
}
```

For exact scope use a JSON array such as `["synthetic-demo"]`. To adopt a dedicated, user-curated export inbox explicitly, set its export source `scope` to `approved-export-folder` instead of `allowlist`; files dropped there are then automatically in scope. Do not aim it at Downloads, a general export directory, or a shared drive. For account/profile discovery see the appropriate built-in source reference. Namespace is an opaque local profile identifier, not a person's email; don't rename it once in use.

```sh
python /absolute/skill/scripts/chat_kb.py --config /absolute/private/config.json scan
python /absolute/skill/scripts/chat_kb.py --config /absolute/private/config.json publish
python /absolute/skill/scripts/chat_kb.py --config /absolute/private/config.json sync
```

- `scan` generates no network traffic. It stages recognized topics in `outbox` and unknown categories in `review` by default. Only keys from the latest successful scan are eligible for publication. Inspect the outbox yourself before authorizing upload. The notification counters do not print originals.
- `publish` sends the latest eligible outbox to the explicitly configured local dataset using `RAGFLOW_API_KEY` from a private process environment. Use the **RAGFlow** key, not the DeepSeek key. Do not paste it into a chat, command history or schedule definition. `sync` is scan and publication in one run, only for a fully approved unattended scope. A nonzero pending status is retryable, not a completed upload. Follow the current API/auth instructions for the target release.
- Timestamp determines a quiet period; it does not determine document identity. The scanner revisits eligible sources and compares a digest of normalized messages and the classification rules; equal or backdated modification times do not hide an edit. Unchanged topics are not rebuilt or re-uploaded. Snapshot-unstable or currently streaming conversations are deferred. Very large histories still have a per-file and per-run cost.
- Classification uses configured keyword groups, not model inference. To replace the defaults, add `"categories": {"Your topic": ["keyword", "another keyword"]}`. Avoid generic words. Widely different clear categories or an explicit “换个话题” start a new note; split/merge ambiguity on an existing note gets counted for review. Word rules cannot reliably separate nuanced topics in the same category.
- State is private and persistent. Back up `scan-state.json` and `ragflow-state.json` together with eligible local files. Keep human annotations outside managed drafts (the scanner rewrites generated Markdown). Removing a source, an allowlist entry, or a file does **not** delete previously published knowledge; review and retire unwanted versions explicitly in RAGFlow. If sensitive content is identified later, remove the relevant published document as well as its held draft; regex checks do not prove absence of secrets.
- The starting test set should contain one chat with two topics, an edited version retaining message IDs, and a second new chat. Check counts, title/classification, search result with source, no duplicate on a repeat, and pending/failure recovery. `fixtures/synthetic-demo.json` in the repository is safe to copy for this purpose.

## Other AI apps / export contract

Convert a visible chat export into **one UTF-8 JSON file per conversation** named `<id>.json`. The `id` and filename stem must match and use only letters, digits, underscore or hyphen. Include timezone-aware ISO 8601 `modified` (for example a source export edit time; don't use the time the scheduler happens to run). Keep a stable message ID across edits and use chronological order.

```json
{
  "id": "synthetic-demo",
  "modified": "2025-01-01T12:00:00Z",
  "messages": [
    {"id":"u1","role":"user","text":"机器人如何判断轮子打滑？"},
    {"id":"a1","role":"assistant","text":"用虚构的编码器与惯导数据比对。"}
  ]
}
```

Only `user` and `assistant` visible text, no tool outputs, system prompts, injected runtime context, internal agents, reasoning, credentials, or attachment file access. Keep export scopes per owner/workspace. Unicode names can go in the message content, not the ID. Normalization determines identity: if a provider has no stable IDs, keep an explicit mapping at export rather than deriving keys from titles or position alone.

