import assert from "node:assert/strict";
import test from "node:test";

import { parseMarkdownJsonArray } from "../lib/markdownImport.mjs";

const characters = [
  {
    name: "林予安",
    gender: "男",
    age: "28",
    occupation: "产品经理",
    is_protagonist: true,
  },
];

test("兼容反引号与 json 之间存在空格", () => {
  const value = `\`\`\` json\n${JSON.stringify(characters)}\n\`\`\``;
  assert.deepEqual(parseMarkdownJsonArray(value), characters);
});

test("兼容标准、大小写和波浪线代码围栏", () => {
  assert.deepEqual(
    parseMarkdownJsonArray(`~~~ JSON\n${JSON.stringify(characters)}\n~~~`),
    characters
  );
});

test("兼容残留语言标记和对象包装", () => {
  assert.deepEqual(
    parseMarkdownJsonArray(`json ${JSON.stringify({ characters })}`),
    characters
  );
});

test("拒绝数组中的非对象人物", () => {
  assert.throws(
    () => parseMarkdownJsonArray("[1]"),
    /第 1 项必须是人物对象/
  );
});
