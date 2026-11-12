/* The calendar (/app/calendar?view=week&date=2026-10-05): meetings by day, week or month, from classic Outlook's
   calendar (read only) and the invitations that came by mail. A meeting opens beside the calendar with the mail
   about it. Keys: t today, d / w / m day, week, month, ← / → back and on. */

import { h, icon, plural } from "./dom.js";
import { clockTime as timeLabel, meetingTime } from "./format.js";

const enc = encodeURIComponent;
const DAY_MS = 86400000;
const WEEKDAYS = [["Monday", "Mon"], ["Tuesday", "Tue"], ["Wednesday", "Wed"], ["Thursday", "Thu"], ["Friday", "Fri"], ["Saturday", "Sat"], ["Sunday", "Sun"]];
const VIEWS = [["day", "Day"], ["week", "Week"], ["month", "Month"]];

/* ---------- Dates, as the app's time zone writes them ("2026-10-05", "2026-10-05T09:30") ---------- */

const parseDay = (text) => {
  const [y, m, d] = text.slice(0, 10).split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d));
};
const dayText = (date) => date.toISOString().slice(0, 10);
const addDays = (date, n) => new Date(date.getTime() + n * DAY_MS);
const mondayOf = (date) => addDays(date, -((date.getUTCDay() + 6) % 7));
const minutesOf = (stamp) => Number(stamp.slice(11, 13)) * 60 + Number(stamp.slice(14, 16));
const fmt = (date, options) => date.toLocaleDateString(undefined, { timeZone: "UTC", ...options });

/** The days a view shows around ``date``: its first and last. */
export function rangeOf(view, date) {
  const day = parseDay(date);
  if (view === "day") return [day, day];
  if (view === "week") {
    const monday = mondayOf(day);
    return [monday, addDays(monday, 6)];
  }
  const first = new Date(Date.UTC(day.getUTCFullYear(), day.getUTCMonth(), 1));
  const start = mondayOf(first);
  return [start, addDays(start, 41)];
}

function step(view, date, by) {
  const day = parseDay(date);
  if (view === "day") return dayText(addDays(day, by));
  if (view === "week") return dayText(addDays(day, 7 * by));
  return dayText(new Date(Date.UTC(day.getUTCFullYear(), day.getUTCMonth() + by, 1)));
}

function title(view, date) {
  const [first, last] = rangeOf(view, date);
  if (view === "day") return fmt(first, { weekday: "long", month: "long", day: "numeric", year: "numeric" });
  if (view === "month") return fmt(parseDay(date), { month: "long", year: "numeric" });
  const sameMonth = first.getUTCMonth() === last.getUTCMonth();
  const end = sameMonth ? String(last.getUTCDate()) : fmt(last, { month: "long", day: "numeric" });
  return `${fmt(first, { month: "long", day: "numeric" })} – ${end}, ${last.getUTCFullYear()}`;
}

/** The meetings on one day: all-day ones covering it, then the others starting it, earliest first. */
function onDay(meetings, day) {
  const text = dayText(day);
  return meetings.filter((m) => (m.all_day ? m.start <= text && m.end > text : m.start.slice(0, 10) === text));
}

/* ---------- The page ---------- */

export function calendarView(data, route, ctx) {
  const view = VIEWS.some(([v]) => v === route.calView) ? route.calView : "week";
  const date = route.date || data.today;
  const go = (nextView, nextDate) => ctx.navigate(`/app/calendar?${new URLSearchParams({ view: nextView, date: nextDate })}`);
  const panel = h("aside", { class: "cal-panel", "aria-live": "polite", "aria-label": "Meeting" }, h("p", { class: "muted small" }, "Choose a meeting to see its details and the mail about it."));
  // The open meeting is in the address, so coming back from its mail (Esc, Back) opens it again.
  const open = (meeting) => {
    for (const chipEl of page.querySelectorAll(".cal-chip")) {
      const on = chipEl.dataset.id === meeting.id;
      chipEl.classList.toggle("open", on);
      if (on) chipEl.setAttribute("aria-current", "true");
      else chipEl.removeAttribute("aria-current");
    }
    if (ctx.remember) ctx.remember(`/app/calendar?${new URLSearchParams({ view, date, meeting: meeting.id })}`);
    showMeeting(panel, meeting, ctx);
  };
  let page = null;
  const bar = h(
    "div",
    { class: "rd-bar cal-bar" },
    h("button", { type: "button", class: "btn btn-sm", title: "Today (t)", onclick: () => go(view, data.today) }, "Today"),
    h("button", { type: "button", class: "icon-btn", title: "Back (←)", "aria-label": `Previous ${view}`, onclick: () => go(view, step(view, date, -1)) }, icon("back", 16)),
    h("button", { type: "button", class: "icon-btn cal-next", title: "On (→)", "aria-label": `Next ${view}`, onclick: () => go(view, step(view, date, 1)) }, icon("back", 16)),
    h("h1", { class: "cal-title" }, title(view, date)),
    h("span", { class: "rd-bar-gap" }),
    h(
      "div",
      { class: "seg", role: "group", "aria-label": "Show" },
      VIEWS.map(([key, label]) => h("button", { type: "button", class: `seg-btn${key === view ? " on" : ""}`, "aria-pressed": String(key === view), title: `${label} (${key[0]})`, onclick: () => go(key, date) }, label))
    )
  );
  const body = view === "month" ? monthGrid(data, date, open, (day) => go("day", dayText(day))) : timeGrid(data, view, date, open);
  page = h("article", { class: "rd cal" }, bar, sourceNote(data.source), h("div", { class: "cal-main" }, h("div", { class: "cal-body" }, body), panel));
  page.keys = (event) => {
    const keys = { t: () => go(view, data.today), d: () => go("day", date), w: () => go("week", date), m: () => go("month", date), ArrowLeft: () => go(view, step(view, date, -1)), ArrowRight: () => go(view, step(view, date, 1)) };
    if (!keys[event.key]) return false;
    event.preventDefault();
    keys[event.key]();
    return true;
  };
  // Arriving with a meeting in the address (from Today, an answer, or back from its mail): it opens, and keys go to
  // it once the page is on screen.
  const meeting = route.meeting ? data.meetings.find((m) => m.id === route.meeting) : null;
  if (meeting) open(meeting);
  page.focusMeeting = () => {
    const heading = meeting && panel.querySelector(".cal-panel-title");
    if (!heading) return false;
    heading.focus({ preventScroll: true });
    panel.scrollIntoView({ block: "nearest" });
    return true;
  };
  return page;
}

function sourceNote(source) {
  if (source.calendar && !source.calendar_last) {
    return h("p", { class: "cal-note muted small" }, icon("info", 13), " Your Outlook calendar hasn't been read yet: it's read with the next sync. Meeting invitations in your mail show here meanwhile.");
  }
  if (source.calendar) {
    return h("p", { class: "cal-note muted small" }, icon("ok", 13), ` Your Outlook calendar, read only; read ${source.calendar_last}. Meeting invitations in your mail show here too.`);
  }
  if (source.outlook_available) {
    return h(
      "p",
      { class: "cal-note muted small" },
      "Showing the meetings from invitations in your mail. ",
      h("a", { href: "/app/settings#set-outlook", dataset: { nav: "" } }, source.connected ? "Turn on Read my calendar too" : "Connect Outlook"),
      " to see your whole Outlook calendar here."
    );
  }
  return h("p", { class: "cal-note muted small" }, "Showing the meetings from invitations in your mail (drop folder). Reading Outlook's calendar directly needs classic Outlook on Windows.");
}

function chip(meeting, open, { time = true } = {}) {
  const said = `${meetingTime(meeting)} · ${meeting.subject}${meeting.location ? ` · ${meeting.location}` : ""}${meeting.cancelled ? " (cancelled)" : ""}`;
  return h(
    "button",
    {
      type: "button",
      class: `cal-chip${meeting.cancelled ? " cancelled" : ""}${meeting.all_day ? " all-day" : ""}`,
      title: said,
      // Its whole name, also where a phone's month shows the meeting as a dot.
      "aria-label": said,
      dataset: { id: meeting.id },
      onclick: () => open(meeting),
    },
    time && !meeting.all_day ? h("span", { class: "cal-time" }, timeLabel(meeting.start)) : null,
    h("span", { class: "cal-subj" }, meeting.subject),
    meeting.cancelled ? h("span", { class: "sr-only" }, " (cancelled)") : null
  );
}

function monthGrid(data, date, open, showDay) {
  const [first] = rangeOf("month", date);
  const month = parseDay(date).getUTCMonth();
  const rows = [];
  for (let week = 0; week < 6; week += 1) {
    const cells = [];
    for (let i = 0; i < 7; i += 1) {
      const day = addDays(first, week * 7 + i);
      const meetings = onDay(data.meetings, day);
      const shown = meetings.slice(0, 3);
      cells.push(
        h(
          "td",
          { class: `cal-cell${day.getUTCMonth() !== month ? " other" : ""}${dayText(day) === data.today ? " today" : ""}` },
          h("button", { type: "button", class: "cal-daynum", "aria-label": fmt(day, { weekday: "long", month: "long", day: "numeric" }) + (meetings.length ? `, ${plural(meetings.length, "meeting")}` : ""), onclick: () => showDay(day) }, String(day.getUTCDate())),
          ...shown.map((m) => chip(m, open)),
          meetings.length > 3 ? h("button", { type: "button", class: "cal-more", onclick: () => showDay(day) }, `+${meetings.length - 3} more`) : null
        )
      );
    }
    rows.push(h("tr", null, ...cells));
  }
  // A phone shows the weekdays as one letter.
  const head = WEEKDAYS.map(([long, short]) => h("th", { scope: "col", abbr: long }, h("span", { class: "cal-wd" }, short), h("span", { class: "cal-wd-1", "aria-hidden": "true" }, short[0])));
  return h("table", { class: "cal-month" }, h("thead", null, h("tr", null, head)), h("tbody", null, ...rows));
}

function timeGrid(data, view, date, open) {
  const [first, last] = rangeOf(view, date);
  const days = [];
  for (let day = first; day <= last; day = addDays(day, 1)) days.push(day);
  const timed = data.meetings.filter((m) => !m.all_day);
  // 7am to 7pm, stretched to the earliest and latest meeting shown.
  const starts = timed.map((m) => minutesOf(m.start));
  const ends = timed.map((m) => (m.end.slice(0, 10) > m.start.slice(0, 10) ? 24 * 60 : minutesOf(m.end)));
  const from = Math.min(7 * 60, ...starts.map((m) => Math.floor(m / 60) * 60));
  const to = Math.max(19 * 60, ...ends.map((m) => Math.ceil(m / 60) * 60));
  const hourPx = 48;
  const hours = [];
  for (let m = from; m < to; m += 60) hours.push(m);
  const head = h(
    "div",
    { class: "cal-days-head", style: { gridTemplateColumns: `56px repeat(${days.length}, minmax(0, 1fr))` } },
    h("span", null),
    ...days.map((day) => h("div", { class: `cal-dayhead${dayText(day) === data.today ? " today" : ""}` }, h("span", { class: "muted small" }, fmt(day, { weekday: "short" })), h("b", null, String(day.getUTCDate()))))
  );
  const allDay = h(
    "div",
    { class: "cal-allday", style: { gridTemplateColumns: `56px repeat(${days.length}, minmax(0, 1fr))` } },
    h("span", { class: "muted small" }, "All day"),
    ...days.map((day) => h("div", { class: "cal-allday-cell" }, onDay(data.meetings, day).filter((m) => m.all_day).map((m) => chip(m, open))))
  );
  const columns = days.map((day) => {
    const meetings = onDay(timed, day);
    const lanes = layout(meetings);
    // A list of the day's meetings, each a button in its place on the day.
    return h(
      "ul",
      { class: `cal-col${dayText(day) === data.today ? " today" : ""}`, style: { height: `${((to - from) / 60) * hourPx}px` }, "aria-label": fmt(day, { weekday: "long", month: "long", day: "numeric" }) },
      ...meetings.map((m) => {
        const top = ((minutesOf(m.start) - from) / 60) * hourPx;
        const end = m.end.slice(0, 10) > m.start.slice(0, 10) ? to : minutesOf(m.end);
        const height = Math.max(20, ((end - minutesOf(m.start)) / 60) * hourPx - 2);
        const { lane, of } = lanes.get(m.id);
        const block = chip(m, open, { time: true });
        block.classList.add("cal-block");
        if (height < 40) block.classList.add("short"); // time and title on one line
        // Lines of title that fit the block's height; the last one ends in "…".
        else block.style.setProperty("--cal-lines", String(Math.max(1, Math.floor((height - 22) / 15))));
        return h("li", { class: "cal-slot", style: { top: `${top}px`, height: `${height}px`, left: `calc(${(lane / of) * 100}% + 2px)`, width: `calc(${100 / of}% - 4px)` } }, block);
      })
    );
  });
  const grid = h(
    "div",
    { class: "cal-grid", style: { gridTemplateColumns: `56px repeat(${days.length}, minmax(0, 1fr))` } },
    h("div", { class: "cal-hours" }, hours.map((m) => h("div", { class: "cal-hour", style: { height: `${hourPx}px` } }, timeLabel(`0000-00-00T${String(m / 60).padStart(2, "0")}:00`)))),
    ...columns
  );
  return h("div", { class: `cal-week view-${view}` }, head, allDay, grid);
}

/** Meetings at the same time side by side: each gets a lane, and every meeting in a cluster the cluster's lanes. */
function layout(meetings) {
  const out = new Map();
  const sorted = [...meetings].sort((a, b) => a.start.localeCompare(b.start) || b.end.localeCompare(a.end));
  let cluster = [];
  let clusterEnd = "";
  const flush = () => {
    const lanes = [];
    for (const m of cluster) {
      let lane = lanes.findIndex((end) => end <= m.start);
      if (lane < 0) lane = lanes.push(m.end) - 1;
      else lanes[lane] = m.end;
      out.set(m.id, { lane, of: 0 });
    }
    for (const m of cluster) out.get(m.id).of = lanes.length;
    cluster = [];
  };
  for (const m of sorted) {
    if (cluster.length && m.start >= clusterEnd) flush();
    cluster.push(m);
    clusterEnd = cluster.length === 1 || m.end > clusterEnd ? m.end : clusterEnd;
  }
  flush();
  return out;
}

/* ---------- One meeting, and its mail ---------- */

async function showMeeting(panel, meeting, ctx) {
  const when = `${fmt(parseDay(meeting.start), { weekday: "long", month: "long", day: "numeric" })} · ${meeting.all_day ? "all day" : meetingTime(meeting)}`;
  const facts = [
    ["When", when],
    meeting.location ? ["Where", meeting.location] : null,
    meeting.organizer ? ["Organizer", meeting.organizer] : null,
    meeting.attendees.length ? ["With", meeting.attendees.join(", ")] : null,
    ["From", meeting.source === "outlook" ? "Your Outlook calendar" : meeting.source === "demo" ? "The sample calendar" : `An invitation emailed by ${meeting.sender || "someone"}`],
  ].filter(Boolean);
  const list = h("div", { class: "cal-related" }, h("p", { class: "muted small" }, "Looking for the mail about it…"));
  const ask = h(
    "button",
    { type: "button", class: "btn btn-sm", onclick: () => ctx.ask(`What do I need to know before “${meeting.subject}” (${when})?`, meeting.email_id || undefined) },
    icon("chat", 14),
    "Ask about this meeting"
  );
  panel.replaceChildren(
    h("h2", { class: "cal-panel-title", tabindex: "-1" }, meeting.subject, meeting.cancelled ? h("span", { class: "tag tag-fraud" }, "Cancelled") : null, meeting.recurring ? h("span", { class: "tag" }, "Repeats") : null),
    h("dl", { class: "cal-facts" }, ...facts.flatMap(([k, v]) => [h("dt", null, k), h("dd", null, v)])),
    ask,
    h("h3", { class: "rel-h" }, "Mail about it"),
    list
  );
  panel.scrollIntoView({ block: "nearest" });
  try {
    const detail = await ctx.getJSON(`/api/calendar/${enc(meeting.id)}`);
    list.replaceChildren(
      detail.related.length
        ? h(
            "ul",
            { class: "rel-list" },
            detail.related.map((r) =>
              h(
                "li",
                { class: "rel-row" },
                h(
                  "a",
                  { class: "rel-link", href: `/app/mail/${enc(r.id)}`, dataset: { nav: "" } },
                  h("span", { class: "rel-main" }, h("span", { class: "rel-top" }, h("b", { class: "trunc" }, r.sender), h("span", { class: "muted small" }, r.when)), h("span", { class: "rel-subject trunc" }, r.subject), r.locked ? h("span", { class: "tag tag-fraud" }, "Held: possible fraud") : null)
                )
              )
            )
          )
        : h("p", { class: "muted small" }, "No mail names this meeting or comes from its people near its date.")
    );
  } catch (error) {
    list.replaceChildren(h("p", { class: "muted small" }, error.message));
  }
}
