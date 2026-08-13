function renumberItems(items) {
  return items.map((item, index) => ({ ...item, index: index + 1 }));
}

export function mergeDownloadPreviews(currentPreview, incomingPreview, currentSelectedUrls = new Set()) {
  const mergedItems = [...(currentPreview?.items || [])];
  const positions = new Map(mergedItems.map((item, index) => [item.url, index]));
  const nextSelected = new Set(currentSelectedUrls);

  for (const item of incomingPreview?.items || []) {
    if (!item?.url) continue;
    const existingIndex = positions.get(item.url);
    if (existingIndex === undefined) {
      positions.set(item.url, mergedItems.length);
      mergedItems.push(item);
      if (item.selected) nextSelected.add(item.url);
    } else {
      mergedItems[existingIndex] = item;
    }
  }

  const preview = { ...(currentPreview || {}), ...(incomingPreview || {}), items: renumberItems(mergedItems) };
  const validUrls = new Set(preview.items.map((item) => item.url));
  return {
    preview,
    selectedUrls: new Set([...nextSelected].filter((url) => validUrls.has(url))),
  };
}

export function removeDownloadPreviewItem(currentPreview, currentSelectedUrls, currentSelectedMedia, url) {
  const items = (currentPreview?.items || []).filter((item) => item.url !== url);
  const selectedUrls = new Set(currentSelectedUrls);
  selectedUrls.delete(url);
  const selectedMedia = { ...(currentSelectedMedia || {}) };
  delete selectedMedia[url];

  return {
    preview: { ...(currentPreview || {}), items: renumberItems(items) },
    selectedUrls,
    selectedMedia,
  };
}

export function buildDownloadInputText(downloadText, preview) {
  const previewUrls = (preview?.items || []).map((item) => item.url).filter(Boolean);
  return preview ? previewUrls.join(' ') : downloadText;
}
