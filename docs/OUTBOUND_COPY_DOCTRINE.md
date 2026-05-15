# OUTBOUND COPY DOCTRINE

Trade Growth Studio writes outbound email for local trade and home-service companies with existing but underperforming websites. The offer is not "AI websites." The offer is audit-backed website replacement and managed web operations: mobile-first call/request paths, service-page and service-area architecture, tracking-ready infrastructure, and optional custom quote forms, calculators, and dashboards.

The email should feel like a real person looked at the public site and wrote a short, specific note. It should not feel like a bot, generic agency blast, AI copy, SEO spam, or fake relationship.

## 1. Voice Rules

- Be concise. Say the useful thing and stop.
- Be specific. Mention the business name, site, niche, market, or visible website issue when available.
- Be slightly conversational. Use normal sentences a contractor or owner would actually read.
- Sound like one person, not an agency committee.
- Do not over-polish. Cold email should feel clear and human, not like a brochure.
- Do not use buzzwords.
- Do not use fake warmth. Avoid compliments that were not earned by evidence.
- Do not use fake urgency. No countdowns, scarcity, or invented deadlines.
- Do not use corporate agency language.
- Do not imply the recipient asked for the audit.
- Do not pretend there is a prior relationship.
- Do not over-explain the internal audit process.
- Do not sell "AI." Sell a better website path for calls, quote requests, service pages, tracking, and managed operations.

Good voice:

- "I looked through your public site and the mobile call path stood out."
- "The first screen makes visitors work harder than it should."
- "I put the short version here:"
- "Worth a short walkthrough?"

Bad voice:

- "Our proprietary system detected multiple conversion issues across your digital presence."
- "We can revolutionize your online experience and 10x your leads."
- "Just checking in to circle back on my previous audit."

## 2. Banned Phrases

Do not use these phrases in generated outbound copy:

- "case file"
- "audit notes"
- "audit recorded"
- "our system detected"
- "AI website"
- "AI websites"
- "guaranteed"
- "we can get you ranked"
- "10x"
- "revolutionize"
- "transform your digital presence"
- "I was impressed by"
- "just checking in"
- "circle back"
- "quick question" unless the question is actually specific
- "teardown" unless the recipient explicitly requested one
- "conversion issue" as a repeated label
- "lead-score"
- "stored audit"
- "detected" as the primary evidence word
- "blast"
- "synergy"
- "growth engine"
- "dominate your market"
- "skyrocket"
- "unlock hidden revenue"
- "leave money on the table"
- "industry-leading"
- "cutting-edge"
- "proprietary AI"
- "we noticed your business is struggling"
- "your website is costing you leads"

Also ban unresolved placeholders:

- "[Your Name]"
- "{{ sender_name }}"
- "{{ business_name }}"
- "{{ public_packet_url }}"
- Any raw Jinja braces in final draft output.

## 3. Preferred Phrases

Use plain, owner-facing language like:

- "I looked through your public site"
- "the mobile version stood out"
- "the first screen makes visitors work harder than it should"
- "the call/request path could be cleaner"
- "I put the short version here:"
- "I marked up the main points here:"
- "I made a private page with the screenshots and notes here:"
- "I would start with these fixes"
- "I would tighten these first"
- "worth a short walkthrough?"
- "open to a short walkthrough?"
- "not relevant, no problem"
- "easy to ignore if this is not a priority"
- "from a phone, the next step is harder to find than it should be"
- "the service pages could do more of the selling"
- "the site could make service areas clearer"
- "tracking looks light from the public site"
- "I did not see a clear tap-to-call link"
- "I did not see an obvious quote form"

Use "I did not see" when the evidence is a crawl or visual review and could be imperfect. Use "the public site shows" when the observation is visible and concrete.

## 4. First-Email Structure

Step 1 is the only default live-send email.

Structure:

1. One sentence of context.
2. Public packet link.
3. Three specific issues.
4. Short interpretation.
5. Walkthrough question.
6. Opt-out line handled separately by the sending/footer layer.

Recommended shape:

```text
Hi {first_name_or_there},

I looked through {business_name}'s public site and the mobile call/request path stood out.

I put the short version here:
{public_packet_url}

I would start with:
- {specific issue 1}
- {specific issue 2}
- {specific issue 3}

None of this needs a huge rebuild plan first. The main thing is making it easier for someone on a phone to understand the service, trust the company, and request help.

Worth a short walkthrough?
```

Rules:

- Use 3 issues by default. Use 4 only when all 4 are strong and distinct.
- Do not dump every issue.
- Do not include internal labels such as `visual_review`, `site audit`, or `lead_score`.
- Do not include the compliance footer in the template body if the sender appends it later.
- Public packet link should be in email 1 naturally, not only appended at send time.

## 5. Follow-Up Doctrine

Follow-ups should exist as drafts, but they should not be used automatically yet.

- Step 1 is the only default live-send step.
- Steps 2-4 are reviewable draft assets only.
- No automatic follow-up scheduling until reply handling, suppression, and manual review policies are explicitly approved.
- Follow-ups should not pretend the recipient read the packet.
- Follow-ups should not say "just checking in" or "circle back."
- Follow-ups should add one useful angle, not repeat the same issue list.
- If the first email included the public packet link, follow-ups can reference "the short page" or "the screenshots" without sounding like a system.

Follow-up angles:

- Step 2: mobile call/request path.
- Step 3: service pages, service areas, and trust signals.
- Step 4: close the loop politely with no pressure.

## 6. Issue Phrasing Rules

Each issue should be translated from internal evidence into owner-facing language. The claim must be no stronger than the evidence.

### Mobile Layout

- Avoid: "The mobile layout creates clear friction for visitors trying to evaluate or contact the business."
- Better: "On mobile, the next step is harder to find than it should be."
- Evidence language: "From the saved mobile screenshot, the call/request path is not prominent."
- Claim strength: Moderate. Say what appears in the screenshot. Do not claim lost calls or revenue.

### Hero Section

- Avoid: "The first screen does not establish the service, location, and next action as quickly as it could."
- Better: "The first screen could say what you do, where you do it, and what to do next faster."
- Evidence language: "The first screen does not clearly combine service, location, and primary action."
- Claim strength: Moderate. Safe when based on visual review.

### CTA Clarity

- Avoid: "The call/request path is less prominent than it should be for a high-intent service visitor."
- Better: "The call/request button could be easier to spot."
- Evidence language: "Primary call/request action is missing, buried, or visually weak."
- Claim strength: Moderate. Do not say users cannot contact the business if other contact options exist.

### Header Navigation

- Avoid: "The header/navigation competes with the primary action more than it should."
- Better: "The header could do a cleaner job pointing people to call or request service."
- Evidence language: "Header has no strong primary action, or the action is crowded by other items."
- Claim strength: Moderate. Frame as clarity, not failure.

### Visual Clutter

- Avoid: "The page presents competing elements that make the next step less obvious."
- Better: "There is a lot competing for attention before the visitor gets a clear next step."
- Evidence language: "Multiple competing elements appear above or near the main call/request area."
- Claim strength: Light to moderate. Avoid sounding insulting.

### Readability

- Avoid: "Several sections are harder to scan than they should be for a service buyer."
- Better: "Some sections take more reading than a phone visitor is likely to give them."
- Evidence language: "Dense text, low contrast, small type, or unclear section hierarchy is visible."
- Claim strength: Light to moderate.

### Design Age

- Avoid: "The visual presentation likely weakens the first impression."
- Better: "The site looks like it may not match the quality of the work you want people to expect."
- Evidence language: "Saved screenshot shows dated layout, older styling, or inconsistent polish."
- Claim strength: Light. Do not insult the business.

### Form/Booking Path

- Avoid: "The request/booking path is not obvious enough from the main conversion areas."
- Better: "The quote/request path could be easier to find and complete."
- Evidence language: "No obvious request form or booking path was visible from the reviewed pages."
- Claim strength: Moderate. Use "I did not see" if crawl evidence could miss forms.

### Service Clarity

- Avoid: "The site does not clarify the core services quickly enough for a visitor."
- Better: "A visitor should be able to tell the main services faster."
- Evidence language: "Homepage or reviewed pages do not quickly surface primary services."
- Claim strength: Moderate.

### Trust Signals

- Avoid: "Trust signals are weak, buried, or not organized around the conversion path."
- Better: "Reviews, proof, and trust points could sit closer to the request path."
- Evidence language: "Trust elements are absent, hard to find, or separated from CTA sections."
- Claim strength: Light to moderate.

### Content Depth

- Avoid: "The service content is thin for a high-intent visitor comparing options."
- Better: "The service pages could answer more of the questions a serious buyer would have."
- Evidence language: "Reviewed pages have limited service detail, process detail, FAQs, or service-area detail."
- Claim strength: Moderate. Do not say it hurts ranking unless backed by data.

### SEO Structure

- Avoid: "The service/page structure is thin for local-search and service-specific discovery."
- Better: "The site could use clearer pages for the main services and service areas."
- Evidence language: "Crawl did not find obvious service-page links or service-area structure."
- Claim strength: Moderate. Do not promise rankings.

### Performance Perception

- Avoid: "The page presentation feels heavier than it should on mobile."
- Better: "The mobile page feels heavier than it needs to, especially before someone can call or request service."
- Evidence language: "Visual review or speed signal suggests heavy above-the-fold experience."
- Claim strength: Light unless PageSpeed data supports it.

### Layout Consistency

- Avoid: "The layout lacks consistency between sections, which may weaken perceived polish."
- Better: "The sections do not quite feel like one clean path."
- Evidence language: "Inconsistent spacing, hierarchy, button styles, or section patterns."
- Claim strength: Light.

### Conversion Path

- Avoid: "The route from landing on the site to calling or requesting service is not direct enough."
- Better: "The path from landing on the site to calling or requesting help could be shorter."
- Evidence language: "Primary action is missing, buried, repeated inconsistently, or disconnected from service content."
- Claim strength: Moderate. Avoid "conversion issue" unless needed once.

### PageSpeed

- Avoid: "The audit shows a mobile PageSpeed performance score of 37, a concerning mobile conversion issue."
- Better: "The mobile speed score came back low, so I would check images, scripts, and the first screen before redesigning around it."
- Evidence language: "Stored PageSpeed or fallback speed score was below threshold."
- Claim strength: Strong for the score itself. Light for business impact.

### No Tel Link

- Avoid: "The site audit did not find a one-tap phone link."
- Better: "I did not see a clear tap-to-call link."
- Evidence language: "Crawl did not find a `tel:` link."
- Claim strength: Moderate. Say "I did not see" because crawlers can miss some markup.

### No Form

- Avoid: "The site audit did not verify an embedded request form."
- Better: "I did not see an obvious quote/request form."
- Evidence language: "Crawl did not find forms on reviewed pages."
- Claim strength: Moderate. Do not say no form exists anywhere unless manually verified.

### No Service Pages

- Avoid: "The site crawl did not find obvious service-page links."
- Better: "The main services could use clearer dedicated pages."
- Evidence language: "Crawl did not find obvious service-page links, and URLs did not show service structure."
- Claim strength: Moderate.

### No Analytics

- Avoid: "The site audit did not detect GA4, GTM, or Facebook Pixel tracking."
- Better: "Tracking looks light from the public site, so call/form measurement may be hard to trust."
- Evidence language: "Public crawl did not find common analytics tags."
- Claim strength: Light to moderate. Do not claim they have no tracking at all.

### No Schema

- Avoid: "The site audit did not detect structured schema markup."
- Better: "I did not see basic local business/service markup in the public page source."
- Evidence language: "Crawl did not find JSON-LD schema types."
- Claim strength: Moderate. Do not imply guaranteed SEO results from adding schema.

## 7. Niche-Specific Language

Use niche language only when the niche is known. Keep it concrete.

### Roofing

- "roof replacement"
- "storm damage"
- "repair vs replacement"
- "insurance claim questions"
- "service areas"
- "inspection request"
- "emergency leak call"
- Example: "For a roofing site, I would make roof replacement, repair, storm damage, and inspection requests easier to separate."

### HVAC

- "AC repair"
- "furnace repair"
- "maintenance"
- "replacement estimate"
- "emergency service"
- "seasonal urgency"
- Example: "For HVAC, the mobile path should make AC repair, furnace repair, and replacement estimates easy to choose from fast."

### Plumbing

- "emergency plumbing"
- "water heater"
- "drain cleaning"
- "leak repair"
- "same-day call"
- "request service"
- Example: "For plumbing, a phone visitor should not have to hunt for emergency service, water heater help, or drain cleaning."

### Electrical

- "panel upgrades"
- "EV charger installs"
- "lighting"
- "repair calls"
- "licensed electrician"
- "estimate request"
- Example: "For electrical, I would separate repair calls, panel work, and install projects more clearly."

### Garage Doors

- "spring repair"
- "opener repair"
- "new door estimate"
- "same-day service"
- "emergency repair"
- Example: "Garage door visitors are often looking for spring repair, opener help, or a replacement quote. Those paths should be obvious on mobile."

### Pest Control

- "termite"
- "ants"
- "rodents"
- "bed bugs"
- "inspection"
- "recurring treatment"
- Example: "For pest control, I would make inspection requests and the main pest categories easier to scan."

### Tree Service

- "tree removal"
- "trimming"
- "storm cleanup"
- "emergency removal"
- "estimate request"
- "service area"
- Example: "For tree service, the site should quickly split removal, trimming, storm cleanup, and estimate requests."

### Restoration

- "water damage"
- "fire damage"
- "mold"
- "emergency response"
- "insurance"
- "24/7 call path"
- Example: "For restoration, the emergency call path needs to be unmistakable on the first mobile screen."

### Remodeling/Exteriors

- "siding"
- "windows"
- "exterior renovation"
- "kitchen/bath"
- "project gallery"
- "estimate request"
- Example: "For remodeling and exterior work, the site should make project types, proof, and estimate requests easy to connect."

## 8. Subject-Line Rules

Subject lines should sound specific but not deceptive.

Good subject styles:

- "Website notes for {business_name}"
- "Mobile site notes for {business_name}"
- "{business_name} call path"
- "A few site fixes for {business_name}"
- "{niche} website notes"
- "{business_name} service page path"

Rules:

- Use the business name when available.
- Keep subjects under about 55 characters when possible.
- Do not fake a reply thread. No "Re:" or "Following up" unless true.
- Do not use clickbait.
- Do not use fear.
- Do not promise results.
- Do not imply the recipient requested the audit.
- Avoid "quick question" unless the body asks one specific question.
- Avoid "free audit" unless the packet is truly positioned that way and no strings are implied.

Bad subjects:

- "URGENT: your website is losing leads"
- "We can get you ranked"
- "10x more roofing leads"
- "I found major problems"
- "Re: your website"
- "Quick question" with a generic body

## 9. Public Packet Wording

The public packet URL should feel like a useful reference, not a tracking trick.

Preferred lines:

- "I put the short version here:"
- "I marked up the main points here:"
- "I made a private page with the screenshots and notes here:"
- "I put the screenshots and the main fixes here:"
- "The short page is here:"

Rules:

- Put the URL on its own line.
- Do not call it a "case file."
- Do not call it a "teardown."
- Do not overstate privacy. "Private page" means unlisted/noindex, not secret or secure.
- Do not imply affiliation with the business.
- If no public packet URL is available, do not pretend one exists.

## 10. Compliance-Safe Copy Rules

- Do not imply a prior relationship.
- Do not imply the business requested the audit.
- Do not claim exact traffic loss.
- Do not claim exact revenue loss.
- Do not claim exact lead loss.
- Do not use unsourced statistics.
- Do not claim guaranteed rankings.
- Do not claim guaranteed revenue, leads, calls, appointments, or conversion lift.
- Do not make legal, licensing, insurance, warranty, or certification claims unless independently verified and sourced.
- Do not hide opt-out language.
- Do not make the opt-out hostile or difficult.
- Do not use deceptive subject lines.
- Do not use fake reply formatting.
- Do not mention scraping, lead scoring, databases, or internal systems.
- Do not say "our system detected."
- Do not make negative claims about competitors.
- Do not say a site is broken unless the evidence proves it.
- Prefer "I did not see" or "looks light from the public site" for crawler-based findings.

## 11. Examples

### Good Roofing Email

```text
Subject: Website notes for RidgeLine Roofing

Hi there,

I looked through RidgeLine Roofing's public site and the mobile call/request path stood out.

I put the short version here:
https://example.com/p/ridgeline-roofing/

I would start with:
- Make roof repair, replacement, and storm damage easier to choose from on the first screen.
- Bring the call/request button higher on mobile.
- Add clearer service-area pages so local searches and visitors have a cleaner path.

The main thing is not more decoration. It is making it easier for someone with a roof problem to understand the service, trust the company, and request an inspection.

Worth a short walkthrough?
```

Why it works:

- Specific to roofing.
- Includes packet URL naturally.
- Gives three concrete issues.
- No fake compliment, urgency, or guarantee.

### Good HVAC Email

```text
Subject: Mobile site notes for Northside HVAC

Hi Chris,

I looked through Northside HVAC's public site from a phone-first angle.

I marked up the main points here:
https://example.com/p/northside-hvac/

I would tighten:
- Separate AC repair, furnace repair, maintenance, and replacement estimates more clearly.
- Make the call/request action easier to spot on the first mobile screen.
- Put reviews or trust points closer to the request path.

For HVAC, people usually arrive with a specific problem. The site should help them pick the right path quickly instead of making every visitor read the same general page.

Open to a short walkthrough?
```

Why it works:

- Uses contact name when available.
- Niche-specific without overclaiming.
- Interprets the issues in owner-facing terms.

### Good Plumbing Email

```text
Subject: Plumbing site call path

Hi there,

I looked through your public site and the request path could be cleaner on mobile.

I put the short version here:
https://example.com/p/clearwater-plumbing/

I would start with:
- Make emergency plumbing, water heater, and drain cleaning paths easier to find.
- Add a clearer tap-to-call option.
- Give the quote/request form a more obvious route from the homepage.

For plumbing, a lot of visitors are trying to solve one problem fast. The site can do more of that sorting before they ever call.

Worth a short walkthrough?
```

Why it works:

- Direct and concrete.
- Avoids saying the business is losing leads.
- Uses "could be cleaner" instead of harsh language.

### Good Generic Home-Service Email

```text
Subject: A few site fixes for Green Valley Services

Hi there,

I looked through Green Valley Services' public site and made a short page with the screenshots and main notes.

I put it here:
https://example.com/p/green-valley-services/

I would start with:
- Make the first screen say the main service and next step faster.
- Move the call/request path higher on mobile.
- Build clearer pages for the main services and areas served.

The site does not need to sound fancier. It needs a cleaner path from "what do you do?" to "how do I request help?"

Worth a short walkthrough?
```

Why it works:

- Works when niche data is weak.
- Still specific to the site path.
- Clear offer fit without agency buzzwords.

### Bad Robotic Email

```text
Subject: Quick question

Hi there,

Our system detected multiple conversion issues in your case file. The audit recorded that your website has weak SEO structure, poor PageSpeed, and no optimized conversion path.

Trade Growth Studio builds AI websites that can transform your digital presence and help you 10x your leads. We can get you ranked and revolutionize your online experience.

Just checking in to see if you want to circle back and discuss this guaranteed growth opportunity.

Best,
[Your Name]
```

Why it is bad:

- Uses banned phrases: "quick question," "our system detected," "case file," "audit recorded," "AI websites," "transform your digital presence," "10x," "we can get you ranked," "revolutionize," "just checking in," "circle back," "guaranteed," and `[Your Name]`.
- Sounds automated and generic.
- Makes risky claims about rankings and leads.
- Implies internal surveillance instead of public-site review.
- Gives no public packet link.
- Gives no niche-specific observations.
- Uses fake urgency and agency buzzwords.

## Implementation Guidance

When this doctrine is implemented in `src/outreach_drafts.py` and `templates/outreach/*.txt.j2`:

- Generate owner-facing issue copy before rendering templates.
- Include `public_packet_url` in the generator context.
- Include configured sender identity, but let the send layer own the final compliance footer unless previews intentionally show it.
- Keep Step 1 as the only default live-send email.
- Keep follow-ups as draft-only assets until automatic follow-up policy is approved.
- Run copy QA before any live send queue is created.
