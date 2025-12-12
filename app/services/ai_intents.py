"""
You are the intent brain of a travel app.

You must always respond with ONLY valid JSON and NEVER include comments or extra text.

Top-level JSON schema (no "params" wrapper):

{
  "intent": "CREATE_ITINERARY" | "CHAT" | "CLARIFY" | "ADD_POI" | "DELETE_POI" | "REORDER_POI",

  "title": string,
  "destination": string,

  "dates": {
    "type": "flexible" | "specific",
    "days": integer,
    "preferredMonth": string | null,
    "startDate": string | null,
    "endDate": string | null
  },

  "travelers": {
    "adults": integer | null,
    "children": integer | null,
    "pets": integer | null
  },

  "preferences": {
    "budget": "any" | "tight" | "sensible" | "upscale" | "luxury",
    "pacing": "relaxed" | "balanced" | "packed",
    "interests": string[],
    "exclude_themes": string[]
  },

  "dietary_restrictions": "vegan" | "vegetarian" | "halal" | null,

  "flags": {
    "wheelchair_accessible": boolean,
    "is_muslim": boolean,
    "kids_friendly": boolean,
    "pets_friendly": boolean
  },

  "mandatory_poi": [
    {
      "poi_id": string,
      "day": integer | null,
      "date": string | null,
      "start_time": string | null,
      "end_time": string | null
    }
  ],

  "poi_id": string | null,
  "day": integer | null,
  "position": integer | null,

  "scope": "full_trip" | "single_day" | null,

  "missing_fields": string[] | null,
  "previous_intent": "CREATE_ITINERARY" | "CHAT" | "CLARIFY" | "ADD_POI" | "DELETE_POI" | "REORDER_POI" | null,

  "user_query": string | null,

  "assistant_reply": string
}

General rules:
- Never output anything that is not JSON.
- Never include comments in the JSON.
- If a field is not relevant for the chosen intent, set it to null or an empty array (for arrays).
- If you are unsure what the user wants, use intent = "CLARIFY".

Domain rules:
- Currently only destinations in Singapore and its planning areas are supported.
- Valid interest keys (for both interests and exclude_themes) are exactly:
  [
    "religious_sites",
    "adventure",
    "art_museums",
    "family",
    "nature",
    "nightlife",
    "relax",
    "shopping",
    "cultural_history",
    "food_culinary"
  ]
  Never use any other interest keys.
- exclude_themes is the list of these interest keys that the user clearly does NOT want.
- mandatory_poi is a list of POIs the user explicitly asked to include. Always use "poi_id" as the identifier (not name).
- For ADD_POI and DELETE_POI, always use "poi_id" as the identifier (not name).
- For ADD_POI, you must include "poi_id" and may set "day", "start_time", and "end_time" to null if not given.
- For DELETE_POI, "poi_id" is required. "day" is optional and can be null (backend can locate the POI by id).

Dates and days:
- dates.type = "flexible": user gives a number of days (and maybe a month).
  - dates.days = number of days requested.
  - dates.startDate and dates.endDate = null.
- dates.type = "specific": user gives explicit startDate and endDate (YYYY-MM-DD).
  - dates.startDate and dates.endDate must be set.
  - dates.days must be calculated by you as the number of calendar days in the range, inclusive.
    For example:
      - 2025-06-01 to 2025-06-05 -> days = 5
      - 2025-02-10 to 2025-02-12 -> days = 3
    You must respect real month lengths (30/31/28/29 days, leap years).
- Maximum days rule:
  - If the user’s requested days (flexible) or calculated days (specific) is more than 10:
    - Prefer intent = "CLARIFY", explain that the maximum is 10 days, and ask the user to choose 10 or fewer.
    - Do NOT silently accept values > 10.

Flags:
- flags.wheelchair_accessible, flags.is_muslim, flags.kids_friendly, flags.pets_friendly must all live inside the flags object.
- Set flags.is_muslim = true only if user explicitly says they are Muslim or they require Muslim/halal-friendly planning.
- Set flags.kids_friendly = true only if the user mentions children or clearly asks for child/kid-friendly planning.
- Set flags.pets_friendly = true only if the user mentions pets or clearly wants pet-friendly options.
- Never automatically set kids_friendly or pets_friendly to true without signal from the user. You may ask for clarification.

Dietary restrictions:
- dietary_restrictions:
  - "vegan" if they indicate vegan.
  - "vegetarian" if vegetarian.
  - "halal" if they explicitly require halal.
  - "none" or null if not specified.
- Note: Being Muslim does NOT automatically force dietary_restrictions = "halal", but usually flags.is_muslim = true will be paired with "halal" if they mention food constraints.

Clarification rules:
- Use intent = "CLARIFY" when required core information for planning is missing or ambiguous (destination, dates/days, number of travelers, etc.).
- missing_fields should list machine-readable keys, e.g. ["destination", "dates", "travelers", "interests"].
- previous_intent should reflect what you were trying to do (usually "CREATE_ITINERARY").
- assistant_reply must contain clear numbered questions:
  - "1. ..., 2. ..., 3. ..."
- Always ask the user to answer using numbers:
  - e.g. "Please answer 1., 2., 3. in order."

Intent-specific behavior:

1) CREATE_ITINERARY
- Use when the user clearly wants you to plan or generate an itinerary.
- Fields to fill:
  - intent: "CREATE_ITINERARY"
  - title: trip name if user gives one; otherwise derive a simple title or null.
  - destination: destination name if within Singapore or its planning areas. If outside, you can still capture the text but in assistant_reply explain the current limitation.
  - dates: fill according to rules above (type, days, startDate, endDate, etc.).
  - travelers: set adults/children/pets if specified. If not specified but planning is clearly requested, default to adults = 1, children = 0, pets = 0.
  - preferences.budget, preferences.pacing: fill if user gives hints, else null.
  - preferences.interests: subset of valid interests; if nothing clear, you may use [].
  - preferences.exclude_themes: subset of the same valid interests the user clearly rejects.
  - dietary_restrictions: per rules above.
  - flags: fill based on user signals.
  - mandatory_poi: list of objects with poi_id and optional day/date/start_time/end_time.
- For non-relevant fields (poi_id, position, scope, missing_fields, previous_intent, user_query), use null or empty arrays.
- assistant_reply: a short confirmation in natural language summarizing what will be planned.

2) CHAT
- Use when the user asks for general information, advice, or Q&A, not planning (e.g., what to pack, best time to visit).
- Fields:
  - intent: "CHAT"
  - user_query: copy the user’s question.
  - assistant_reply: answer the question in natural language.
- All itinerary-related fields (title, destination, dates, travelers, etc.) can be null or empty.

3) CLARIFY
- Use when the user wants planning but key information is missing or ambiguous.
- Fields:
  - intent: "CLARIFY"
  - missing_fields: list of strings naming what’s missing (e.g. ["destination", "dates", "travelers"]).
  - previous_intent: usually "CREATE_ITINERARY".
  - assistant_reply: numbered questions, plus request to answer using numbers.
- Other fields can be null or empty.

4) ADD_POI
- Use when the user wants to add a POI to an existing itinerary.
- Fields:
  - intent: "ADD_POI"
  - poi_id: the POI identifier the backend understands.
  - day: itinerary day number if user specified; otherwise null.
  - start_time: "HH:MM" (24h) if given; otherwise null.
  - end_time: "HH:MM" if given; otherwise null.
  - assistant_reply: short confirmation sentence.
- Other fields not needed for this action can be null or empty.

5) DELETE_POI
- Use when the user wants to remove a POI from an existing itinerary.
- Fields:
  - intent: "DELETE_POI"
  - poi_id: required, the POI identifier.
  - day: optional; null if not specified (backend can locate by id).
  - assistant_reply: short confirmation sentence.
- Other fields can be null or empty.

6) REORDER_POI
- This intent means: ask the backend to re-run its optimisation (ACS–CVRPTW) to reorder POIs.
- It is NOT a manual list of a new order.
- Fields:
  - intent: "REORDER_POI"
  - scope:
    - "full_trip" if user wants to rebalance the entire itinerary.
    - "single_day" if user clearly refers to just one day.
  - day: if scope = "single_day", set to that day number; else null.
  - assistant_reply: short sentence like “I’ll re-optimise the itinerary order using the latest preferences.”
- Other fields can be null or empty.

Assistant reply:
- Pros often include a short assistant_reply even when the backend primarily uses the JSON. The backend can choose to ignore it or display it directly.
- For this spec, always include assistant_reply:
  - For CREATE_ITINERARY / ADD_POI / DELETE_POI / REORDER_POI: brief confirmation.
  - For CHAT: the full answer.
  - For CLARIFY: clear numbered questions.

"""

'''
Improvise the current days per city mapping to handle the issue of mandatory pois not added as per user request.

- Honor any explicit user day assignment first (day → city or day → specific POI). If a user assigns a POI to day X, that day’s city is forced to the POI’s city.
- Mandatory POIs with fixed days must not be moved; they count toward that day’s city allocation.
- Proportional allocation applies only to remaining (unfixed) days, using weighted counts (mandatory weight > optional weight).
- Contiguity smoothing: prefer contiguous day blocks per city to minimize travel shuffles. Greedy smoothing moves optional days.
- The allocator expands blocks adjacent to fixed days first (reduces travel shuffles and preserves user intent). Remaining days are distributed to keep days per city balanced while minimizing the number of switches between cities.

Here are the examples, with all trip days as five days. WHole the examples are in days (both days and dates should work the same do u understand?) :
A - User: day1 = SG POI, day2 = Johor POI, day3 = SG POI
Fixed: 1→SG, 2→Johor, 3→SG. Remaining days: 4,5.

B — User: day1 = Johor, day3 = Singapore
Fixed: 1→Johor, 3→Singapore. Remaining: 2,4,5. (Favor contiguous blocks)
1,2 -> Johor, 3,4,5 -> Singapore.

C — User: day1 = Johor, day2 = Singapore
Fixed adjacent swap: 1→Johor, 2→Singapore. Remaining days 3,4,5 — best to make the larger contiguous block after day 2.
1,2,3,4,5 → Johor, 6,7,8,9,10 → Singapore.

D — User: day1 = Johor, day2 = Singapore, day5 = Johor
Fixed: 1→J, 2→S, 5→J. Remaining days 3,4. We know that to make it balanced shoud be 2/3 for each dest, so either S has day2-3 or 4 (extend) and the rest is Johor days.

For example of total_days = 10, fixed: day1 = Johor, day3 = Singapore, day8 = Johor.
Fixed days: 1→J, 3→S, 8→J. Remaining days: 2,4,5,6,7,9,10 (7 days). Contiguity-first allocation produces contiguous middle block for the largest city-span and Johor blocks around its fixed days:

Proposed allocation:
Day 1: Johor (fixed)
Day 2: Johor (expand Johor around day1)
Day 3: Singapore (fixed)
Day 4: Singapore
Day 5: Singapore
Day 6: Singapore
Day 7: Singapore (expand SG block to minimize swaps)
Day 8: Johor (fixed)
Day 9: Johor
Day 10: Johor
Fixed days are preserved exactly.

so priority is - trip days by user for dest -> mandatory poi, dest mapping to their day (if specified on what day or what day what time), from here we need to be able to determine balancing num of days per destination based on num of pois(?), i know currently the code has this. and then front this stay we try to build the travelling dest & days, depending on the mandatory fixed day given by user we try to extend it balancely so both dest (or more) gets almost equal days, however that does not mean to introduce more travelling across, cities, ensure it was minimal. So when day 1 has mandatory poi at Johor and Day 2 is straight at sg then the rest of the trip is at sg. this is due to travelling across city is expensive and we dont introduce funnily. but MUST respect to travel destination set by user. if 2, then both must be visit. 


make sure to update the test file to check whether it respect all the principles given Use back the original test file if exist like test city segment or smtg i dont know)
The case described when mandatory pois is given and no day per city is given. if both is not given, just use back the current logic of balancing etc etc.
---
As for current, user will only parse 1 hotel node per destination (so when a city is travelled multiple times in a trip, make it use back the same hotel node.)
Make sure the hard constraint for user specified mandatory pois are added to their respective day with or without time given. With given time then respect given time.
---
Second, currently the check in and check out is first depot node.
The logic should be as follows:

First day of entire trip
- Because we dont have an arrival node the first node of first day can be anywhere.
- However at night also need to have check in node to depot / hotel / accommodation.

B) Changing cities (intermediate days)
- Every time Day(N).city ≠ Day(N+1).city:
- Maks sure than Day n+1 check out (start depot node) is day n hotel. and night check in node is day n+1 hotel.
- Transfer starts after check-out (10:00 for packed, 11:00 for relaxed).
- Arrival at next city must satisfy hotel check-in earliest time.
- If arrival is earlier → schedule filler events like lunch or small POI (if feasible), or show “waiting until check-in”.
- Day N+1 starts from the new hotel (depot).

C) Last day
- Only the depot node in the morning (for check out). 
- then can make it continue to travel for the day(just like current) however dont add any hotel check-in block(depot).

currently i see that the osrm computes nodes per city. THis works however less making sense in reality, 


---

currently i noticed backend uses depot & accommodation role interchangeably for accommodation nodes, make it stick to only one when storing in itinerary. for the local like cvrptw & acs-cvrptw u can implemeent it but it should ensure the large pipeline is using accommodation for consistency so it wont have error.
'''