# Example agent conversation

An assistant connected to Rainout Source (MCP at `/mcp`, or the OpenAPI actions) and to the user's
Google Calendar. Tool calls are shown in brackets.

---

**User:** I play on ATX Boys, Friday nights at Craig Fields.

**Agent:** *[find_team / searchTeams q="ATX Boys", venue="Craig Fields", sport="softball", day="friday"]*

> Best candidate: **ATX Boys**, Capital City Softball (City of Austin), *Friday Softball 503 Men 0HR
> South*, Krieg Field Softball Complex, confidence 0.91. `next_step: confirm_with_user`.

**Agent:** I found **ATX Boys** in the City of Austin's Friday men's softball league (division 503) at
Krieg Fields. Is that your team?

**User:** Yep.

**Agent:** *[get_games / getTeamGames team_id=tsl-340-744621-4306365, date=today]*

> `count: 0`, `next_game`: Fri Oct 16, 8:20 PM vs The Vatos, Krieg 05, play_probability 96,
> reason: "Your game tomorrow at 8:20 PM vs The Vatos at Krieg Field Softball Complex should be on
> (96% likely): no meaningful rain in the forecast and no cancellation has been posted."

**Agent:** No game today. Your next one is **tomorrow at 8:20 PM vs The Vatos on Krieg 05**. It
should be on (96% likely): no meaningful rain in the forecast, and no cancellation has been posted.
Want me to add it to your Google Calendar?

**User:** Sure.

**Agent:** *[Google Calendar events.import with the game's `calendar_event` (iCalUID
tsl-340-217886410@rainout-agent-source.vercel.app)]* Done. It's on your calendar with the odds
in the notes. I'll use the same event if you ask me to update it later.

---

**User (Friday afternoon):** Do I have a game today?

**Agent:** *[get_games team_id=tsl-340-744621-4306365, date=today]* Yes: **8:20 PM vs The Vatos
at Krieg 05**. Thunderstorms are likely this evening (70% chance of rain), so it's at real risk of a
rainout (34% likely). Call the rainout line, 512-978-2680, before you head out.

---

**User:** What about my baseball team, the Generals?

**Agent:** *[find_team q="Generals", sport="baseball"]* Is that the **Generals** in the Austin Metro
Baseball League (Fall 2026)? *(User: yes.)* *[get_games team_id=ambl-2026fall-30]* Your next game is
**Sunday at 7 PM at the Royals, Downs Field**. It should be on (96% likely).

---

**User:** I'm on the Sandlot Sluggers in a Round Rock league.

**Agent:** *[find_team q="Sandlot Sluggers"]* → `next_step: ask_user_for_schedule_url`

**Agent:** I couldn't find the Sandlot Sluggers in the league schedules I can search. Do you have a
link to your league's schedule, like a calendar (ICS) link or the schedule page?

**User:** https://example-league.org/teams/sluggers.ics

**Agent:** *[find_team q="Sandlot Sluggers", schedule_url=…]* → candidate `url-…` (extraction: ics)
→ confirm with the user → *[get_games]* → same flow as above.

---

Rules the tools enforce in their descriptions: confirm the team match with the user, read
`reason` aloud, and **always ask before adding anything to a calendar**. Rainout Source itself
never writes to a calendar.
