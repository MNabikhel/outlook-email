/* What the classic pages' chat (static/app.js, a plain script) uses from the workspace's modules, so an answer
   is formatted and its citations linked by the same code in both, and the answer's stream read the same way.
   Loaded as a module before app.js, which finds it as window.CloseDeskShared. Also what the CloseDesk window does
   with links that leave CloseDesk (in_window.js), as in the workspace. */

import { fileHref, formatAnswer } from "./format.js";
import { postStream } from "./api.js";
import { isTyping } from "./dom.js";
// In the CloseDesk window, what leaves CloseDesk opens where it would from a browser.
import "./in_window.js";

window.CloseDeskShared = { fileHref, formatAnswer, postStream, isTyping };
