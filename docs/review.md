# Review Mode

Review mode is for practicing and testing knowledge that's been committed to the database. Review sessions follow a general flow guided by the system prompt — phase progression is not enforced programmatically.

## Entering Review Mode

- **Slash command:** `/review`
- **Keybinding:** `Shift+Tab` cycles through modes (idle → learn → review → idle)
- **Agent tool:** The `set_mode` tool can switch modes programmatically

## Session Lifecycle

A review session follows this general flow:

```
SCOPING → CONFIGURING → PLANNING → REVIEWING → FINISHING
```

State is tracked in two places: `ReviewState` (in-memory graph state, cleared on session end) and `ReviewSession` (database record, persists for historical continuity). The `ReviewState` is lazily initialized on the first call to `review_update_session_state`.

### Scoping

The agent helps the user decide what to review:
1. Browse the knowledge base using `list_topics`, `list_knowledge_entries`, `read_knowledge_entries`
2. Check prior review history via `review_get_past_sessions` (retrieves up to 5 sessions overlapping the selected topics, ranked by IoU, with `final_summary` for continuity)
3. Set scope by calling `review_update_session_state(scope=[...entry_ids...])` — this lazily creates the `ReviewSession` DB record and initializes `ReviewState`

### Configuring

The agent sets session parameters via `review_update_session_state(config_update=ReviewConfigUpdate(...))`:

| Parameter | Options | Default behavior |
|-----------|---------|-----------------|
| **Review style** | `flashcard`, `conversation`, `mixed` | Flashcard: structured Q&A. Conversation: open-ended discussion. Mixed: flashcards then conversation. |
| **Critique timing** | `during`, `after` | During: immediate feedback. After: batched at end. |
| **Ephemeral** | bool | If true, session won't appear in future `review_get_past_sessions` calls |
| **User instructions** | text | Stored on the session for agent reference |

The agent infers what it can from context and only asks about ambiguous options. Config fields can be set all at once or individually.

### Planning

The agent prepares the question sequence before starting:

**For flashcard-based reviews:**
1. `list_flashcards(knowledge_entry_ids=[...])` — check which entries already have flashcards
2. `read_flashcards(flashcard_ids)` — inspect existing card content
3. `review_update_session_state(flashcards=ReviewFlashcardUpdate(action="append", flashcard_ids=[...]))` — queue existing flashcard IDs
4. For entries that need new flashcards, follow the proposal workflow:
   - `flashcard_proposal_create(flashcards)` — stage cards for user review
   - `flashcard_proposal_present()` — show proposal to user (approve / edit / cancel)
   - `flashcard_proposal_accept()` — write approved cards to DB
   - `review_update_session_state(flashcards=ReviewFlashcardUpdate(action="append", flashcard_ids=[...]))` — add created IDs to the queue
5. Use `ReviewFlashcardUpdate(action="set", flashcard_ids=[...])` to replace the full queue order if needed

**For conversational reviews:**
- Load entry content and organize a discussion flow
- No flashcards created — questions are generated naturally during the review

Optionally call `review_update_session_state(plan="...")` to store a discussion plan outline.

### Reviewing

The core loop. The agent presents questions, evaluates responses, and records interactions.

**Flashcard flow:** Pop from queue → present question → receive answer → score → record → repeat until queue empty.

**Conversational flow:** Follow the discussion plan → ask natural questions → evaluate understanding → record at checkpoints.

**Scoring:** 1–4 scale matching the flashcard rubric (1 = again, 2 = hard, 3 = good, 4 = easy), aligned with FSRS `Rating` values. The agent judges core understanding rather than expecting verbatim recall or completeness on peripheral details.

**Recording:** `review_record_interaction` records a review checkpoint with `entry_ids`, `score`, and an optional `summary`. Creates `ReviewInteraction` + `ReviewInteractionEntry` DB records and updates in-memory entry coverage tracking.

### Finishing

`review_finish_session(agent_summary="...")` computes aggregate stats, combines them with the agent's observations into a final summary, persists it to the DB (unless ephemeral), marks the session complete, and clears `ReviewState`. The tool returns the stats for the agent to present to the user.

## Flashcard Model

Flashcards are reusable question templates stored in the database:
- Tied to a **topic** (required) and optionally to the **session** that created them
- Linked to one or more **knowledge entries** via `FlashcardEntry` junction (enables synthesis questions)
- Have `question_text`, `answer_text`, and optional `testing_notes` (instructions for evaluating responses)
- When a parent session is deleted, its flashcards cascade-delete

The flashcard queue supports append, set, remove, and clear operations via `ReviewFlashcardUpdate`.

## Review Tools

| Tool | Purpose |
|------|---------|
| `review_get_past_sessions` | Retrieve prior sessions for continuity |
| `review_show_session_state` | Dump current session state |
| `review_update_session_state` | Update scope, config, flashcard queue, plan, or clear state |
| `review_record_interaction` | Record conversational Q&A, update coverage |
| `review_present_flashcards` | Present flashcards via widget, handle scoring |
| `review_finish_session` | Compute stats, persist summary, clear state |

## Other Available Tools

**Database (read-only):** `list_topics`, `list_knowledge_entries`, `read_knowledge_entries`, `list_flashcards`, `read_flashcards`
**App:** `update_app_state`, `set_mode`, `ask_user_input`
**Web:** `web_search`, `web_fetch`
