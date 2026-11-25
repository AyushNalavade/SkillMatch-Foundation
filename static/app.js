let modalOpener = null;

// Python and HTMX cannot dismiss local modal DOM or restore browser focus without a request.
document.addEventListener("click", (event) => {
  if (!(event.target instanceof Element)) return;
  const trigger = event.target.closest('[aria-haspopup="dialog"]');
  if (trigger instanceof HTMLElement) modalOpener = trigger;
  const clickedBackdrop = event.target.matches("[data-modal-overlay]");
  const clickedCloseButton = event.target.closest("[data-modal-close]");
  if (!clickedBackdrop && !clickedCloseButton) return;
  closeModal();
});

// The browser must move focus into the dialog after HTMX inserts it.
document.body.addEventListener("htmx:afterSwap", (event) => {
  if (event.detail.target?.id !== "modal-root") return;
  const dialog = document.querySelector('#modal-root [role="dialog"]');
  if (dialog instanceof HTMLElement) dialog.focus();
  else if (modalOpener?.isConnected) {
    modalOpener.focus();
    modalOpener = null;
  }
});

// Python/HTMX cannot trap keyboard focus or handle Escape within the local page DOM.
document.addEventListener("keydown", (event) => {
  const dialog = document.querySelector('#modal-root [role="dialog"]');
  if (!(dialog instanceof HTMLElement)) return;
  if (event.key === "Escape") {
    event.preventDefault();
    closeModal();
    return;
  }
  if (event.key !== "Tab") return;

  const focusable = Array.from(dialog.querySelectorAll(
    'a[href],button:not([disabled]),input:not([disabled]):not([type="hidden"]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])'
  )).filter((element) => element.getClientRects().length > 0);
  if (!focusable.length) {
    event.preventDefault();
    dialog.focus();
    return;
  }
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  const focusIsOutside = !dialog.contains(document.activeElement);
  if (event.shiftKey && (document.activeElement === first || document.activeElement === dialog || focusIsOutside)) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && (document.activeElement === last || document.activeElement === dialog || focusIsOutside)) {
    event.preventDefault();
    first.focus();
  }
});

// Python and HTMX cannot restore the opener's focus after a DOM-only dismissal.
function closeModal() {
  const modalRoot = document.getElementById("modal-root");
  if (!modalRoot?.hasChildNodes()) return;
  modalRoot.replaceChildren();
  if (modalOpener?.isConnected) modalOpener.focus();
  modalOpener = null;
}

// Clipboard access has no Python or HTMX equivalent.
async function copyEventLink(event) {
  if (!(event.target instanceof Element)) return;
  const button = event.target.closest("[data-copy-event-link]");
  if (!button) return;

  try {
    await navigator.clipboard.writeText(button.dataset.copyEventLink);
    button.textContent = "Link copied";
  } catch {
    button.textContent = "Copy unavailable";
  }
}

document.addEventListener("click", copyEventLink);
