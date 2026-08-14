function unwrapMarkdownCodeFence(value) {
  let raw = String(value || "").replace(/^\uFEFF/, "").trim();
  const fenced = raw.match(
    /^\s*(`{3,}|~{3,})[ \t]*([^\r\n]*)\r?\n([\s\S]*?)\r?\n[ \t]*\1[ \t]*$/
  );
  if (fenced) raw = fenced[3].trim();

  // 兼容围栏未被上游剥离时残留的语言标记，例如 “json [...]”。
  raw = raw.replace(/^\s*(?:json|javascript|js)\s*(?=[[{])/i, "").trim();
  raw = raw.replace(/;\s*$/, "").trim();

  // 兼容 JSON 前后带有一行简短说明的 Markdown。
  if (!raw.startsWith("[") && !raw.startsWith("{")) {
    const arrayStart = raw.indexOf("[");
    const arrayEnd = raw.lastIndexOf("]");
    if (arrayStart >= 0 && arrayEnd > arrayStart) {
      raw = raw.slice(arrayStart, arrayEnd + 1).trim();
    }
  }
  return raw;
}

export function parseMarkdownJsonArray(value) {
  const raw = unwrapMarkdownCodeFence(value);
  if (!raw) return null;

  const parsed = JSON.parse(raw);
  const items = Array.isArray(parsed)
    ? parsed
    : (parsed && Array.isArray(parsed.characters) ? parsed.characters : null);
  if (!items) throw new Error("内容必须是 JSON 数组，或包含 characters 数组的 JSON 对象");

  items.forEach((item, index) => {
    if (!item || typeof item !== "object" || Array.isArray(item)) {
      throw new Error(`第 ${index + 1} 项必须是人物对象`);
    }
  });
  return items;
}

