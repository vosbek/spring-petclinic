# Exception for <check id>

Add to `.fence/exceptions.json` (a person does this, with their own name):

```json
{
  "check": "<check id, e.g. duplicates or lint:patterns>",
  "file": "<file, or a folder ending in />",
  "reason": "<why this case is legitimate and why it is temporary>",
  "approvedBy": "<the person's name; never filled in by an agent>",
  "expires": "<YYYY-MM-DD, at most 90 days from today>"
}
```

What will remove the need for it before it expires: <the follow-up change and who owns it>
