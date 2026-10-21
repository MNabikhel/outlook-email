/* What the classic pages' chat (static/app.js, a plain script) uses from the workspace's modules, so an answer
   is formatted and its citations linked by the same code in both, and the answer's stream read the same way.
   Loaded as a module before app.js, which finds it as window.CloseDeskShared. */

import { fileHref, formatAnswer } from "./format.js";
import { postStream } from "./api.js";
import { isTyping } from "./dom.js";

window.CloseDeskShared = { fileHref, formatAnswer, postStream, isTyping };
