---
title: The Knowledge page
parent: User guide
nav_order: 6
---
# The Knowledge page

clanker-learn keeps a small knowledge base about you, which every project's tutor reads: your goals, how you want to be taught, what you know already, the lessons you finished and what the tutors learned about how you learn. **Knowledge**, at the bottom of the sidebar, shows all of it, and you can change or delete any of it. Its dot is lit when a tutor proposes something that waits for you.

The page's own list has three parts: **About you, everywhere** (Your briefs, Global insights), **Per project** (one entry for each project's insights) and **Everything** (Lessons done, History).

## Your briefs

A brief is what a tutor is told about you when a lesson starts. **Overview** shows its parts; **As a tutor reads it** shows the exact text a tutor is given, put together from the records by the program, never written by a model. A brief is never shortened, whatever its length; a long one takes more of each lesson's context, which the [context line](lessons.md#the-context-line) shows.

| Part | What it is | Who writes it |
|---|---|---|
| **Your background** | What you know already and what you work with, in a box of any length. **Save** it; every tutor reads it as you wrote it from its next lesson. | You only. |
| **Your goals** | What you want to be able to do. | You, or a tutor quoting you. |
| **How you want to be taught** | Your preferences. | You, or a tutor quoting you. |
| **How you learn** | The [global insights](#global-insights) you approved. The same in every brief set. | A tutor proposes, you approve. |
| **Recently** | The last ten lessons done in the projects that use the set, one line each; open one for its summary. | clanker-learn. |

A tutor records a goal or a preference only with a sentence you typed in a lesson, quoted exactly (12 characters at least), never one it inferred. Each line says who recorded it, and **What it rests on** shows your sentence. Add your own in your own words under each list, and edit or delete any line.

## Brief sets

You can keep several brief sets: one for work and one for a hobby, say, or one per person who learns here. Each set has its own background, goals, preferences and "Recently"; the global insights are shared by every set.

The **Brief set** card at the top of Your briefs picks the set shown, and:

| To | Press |
|---|---|
| Make a set, empty or a copy of the one shown | **New** |
| Rename the one shown | **Rename** |
| Delete the one shown, with its goals, preferences and background | **Delete**. Only when no project uses it; the History cannot undo it. |

Each project uses one set: chosen in the New project box (**Brief**) and changed in the project's [Config box](projects.md#the-config-box), where a lesson takes the change at its next start. **Default** holds what there was before sets existed, and is used by every project not given another; it cannot be deleted.

## Archiving a goal

A goal you have reached, or put aside, can be archived instead of deleted: **Archive** beside it takes it out of every brief and out of what a tutor can look up, and keeps it here. Tutors are not told about archived goals. **show archived** above Your goals lists them; **Restore** puts one back in the brief. Only you archive and restore goals, and both are in the [History](#history-and-undo), where they can be undone.

## Global insights

Global insights are the few things every tutor, in every project, is told about how you learn. A tutor can only propose one: a new one, a replacement, or the retirement of one. You can also add one yourself. There is no limit on how many.

A proposal waits under **Waiting for you**, with the project and lesson it came from, why, what it rests on, and the like insights of your other projects. Edit its text as you want every tutor to read it, then:

| Press | What happens |
|---|---|
| **Approve** | Every tutor is told it from its next message. |
| **Dismiss** | Nothing changes; the tutor that proposed it hears of it with your next message there. |

## Project insights

What a project's tutors learned about how you learn there (a misconception, a strength, what worked, what did not) is under that project's entry in **Per project**, and in the project's **Insights** tab. Each rests on something clanker-learn itself saw: a check that ran, an answer you gave, a lesson's summary, or a sentence you typed; **What it rests on** shows it. Only that project's tutors are told them. A tutor changes one only by replacing or retiring it; **show retired** shows those too.

A destroyed project's insights and lessons stay here, marked as removed, unless you chose to forget them when you destroyed it; no tutor is told them.

## Lessons done

Every lesson a tutor closed, in every project, newest first, with a search over the titles and summaries. The line in bold of a summary is what later lessons' tutors are given.

## History and undo

**History** lists every change to what the tutors know about you, by you or by a tutor, newest first, with what it was before and after. **Undo** takes a change back, unless what it changed was changed again since.

How it works: [Knowledge](../architecture/knowledge.md)
