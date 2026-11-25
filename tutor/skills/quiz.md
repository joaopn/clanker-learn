---
name: quiz
description: A short run of quick questions on what this lesson covered, or on a topic you name
argument-hint: "[n] [topic]"
---
Quiz the learner: n quick questions (5 when no number is given, at most 10), on the topic they named, or else on what this lesson covered so far (read `notes.md` and the conversation).
- Ask them with `quiz_ask`, one at a time: the next only after they answered the last. Grade an answer they typed with `answer_grade`.
- Between questions, only a one-line correction when they got it wrong: no new teaching.
- At the end, give the score and the weak spots, in a few lines.
- When the run shows something about how the learner learns (a misconception, a solid strength), record it with `insight_record`, resting on the cards and `answer_grade` evidence.
