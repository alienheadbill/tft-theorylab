// Cached game art (portraits, item and trait icons). The API hands out
// same-origin /static/game/... URLs from the committed manifest; the page
// never asks CommunityDragon for anything. Every image sits on top of a
// text fallback (initials or a letter) that shows whenever there's no art.

const artUrl = url => (typeof url === 'string' && url.startsWith('/static/game/') ? url : null);

// Decorative: the name is always written right next to it, so alt="".
const artImg = (url, size) => {
  const src = artUrl(url);
  return src
    ? `<img src="${src.replace(/[&"<>]/g, ch => `&#${ch.charCodeAt(0)};`)}" alt="" width="${size}" height="${size}" loading="lazy" decoding="async">`
    : '';
};

// A missing or broken file: drop the <img> so the fallback underneath shows
// instead of a broken-image icon. `error` doesn't bubble, hence capture.
document.addEventListener(
  'error',
  event => {
    const img = event.target;
    if (!(img instanceof HTMLImageElement)) return;
    const holder = img.closest('.has-art');
    if (!holder) return;
    if (holder.classList.contains('art-optional')) {
      holder.remove(); // an inline chip beside text: the text alone is the fallback
      return;
    }
    holder.classList.remove('has-art');
    img.remove();
  },
  true,
);

// A small inline chip before a name; nothing at all when there's no art.
const artChip = (url, kind = '') =>
  artUrl(url) ? `<span class="art-chip ${kind} has-art art-optional" aria-hidden="true">${artImg(url, 22)}</span>` : '';
