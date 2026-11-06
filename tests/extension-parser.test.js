'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const {
  START, END, parseCommandBlock, parseVisibleCommandBlocks, renderedResponseSignature, claimOnce,
} = require('../extension/command-parser.js');
const commandText = (name = 'ArenaSmoke') => `${START}\n{"operation":"create_test_folder","name":"${name}"}\n${END}`;
const block = (text, element = {}, visible = true) => ({ text, element, visible });

test('parser accepts one visible, well-formed create-test-folder block', () => {
  assert.deepEqual(parseVisibleCommandBlocks([block(commandText())]), {
    kind: 'command',
    command: { operation: 'create_test_folder', name: 'ArenaSmoke' },
    stats: { visibleCodeBlocks: 1, commandBlocks: 1, duplicateNodeReferences: 0, nestedEquivalentCopies: 0 },
  });
});
test('create_part parser accepts the exact bounded schema and rejects malformed values', () => {
  const valid = { operation: 'create_part', name: 'ArenaPlatform', position: { x: 0, y: 5, z: -10 }, size: { x: 12, y: 1, z: 12 }, color: { x: 80, y: 160, z: 220 }, material: 'SmoothPlastic', anchored: true, canCollide: true };
  assert.deepEqual(require('../extension/command-parser.js').validateCommand(valid), valid);
  const invalid = [
    { ...valid, position: { x: 10001, y: 0, z: 0 } }, { ...valid, position: { x: NaN, y: 0, z: 0 } },
    { ...valid, size: { x: 0, y: 1, z: 1 } }, { ...valid, size: { x: Infinity, y: 1, z: 1 } },
    { ...valid, color: { x: 256, y: 0, z: 0 } }, { ...valid, color: { x: 1.5, y: 2, z: 3 } },
    { ...valid, material: 'Air' }, { ...valid, name: '../bad' }, { ...valid, anchored: 1 },
    { ...valid, canCollide: 'true' }, { ...valid, extra: 'x' }, { ...valid, position: { x: 0, y: 0 } },
    { ...valid, size: '12,1,12' },
  ];
  for (const value of invalid) assert.equal(require('../extension/command-parser.js').validateCommand(value), null);
});
test('same DOM element referenced twice is counted once, but reports the duplicate reference', () => {
  const element = {};
  const parsed = parseVisibleCommandBlocks([
    block(commandText(), element),
    block(commandText(), element),
  ]);
  assert.equal(parsed.kind, 'command');
  assert.equal(parsed.stats.visibleCodeBlocks, 1);
  assert.equal(parsed.stats.commandBlocks, 1);
  assert.equal(parsed.stats.duplicateNodeReferences, 1);
});
test('nested wrappers exposing the same rendered command collapse to one rendered block', () => {
  const inner = {};
  const outer = { contains: other => other === inner };
  const parsed = parseVisibleCommandBlocks([
    block(commandText(), outer),
    block(commandText(), inner),
  ]);
  assert.equal(parsed.kind, 'command');
  assert.equal(parsed.stats.commandBlocks, 2);
  assert.equal(parsed.stats.nestedEquivalentCopies, 1);
});
test('two genuinely separate visible command blocks are rejected even when identical', () => {
  const parsed = parseVisibleCommandBlocks([
    block(commandText(), {}),
    block(commandText(), {}),
  ]);
  assert.equal(parsed.kind, 'invalid');
  assert.equal(parsed.error, 'multiple_command_blocks');
  assert.equal(parsed.stats.commandBlocks, 2);
});
test('nested but conflicting commands and mixed valid/malformed blocks fail closed', () => {
  const inner = {};
  const outer = { contains: other => other === inner };
  assert.equal(parseVisibleCommandBlocks([
    block(commandText('First'), outer), block(commandText('Second'), inner),
  ]).error, 'nested_ambiguous_blocks');
  assert.equal(parseVisibleCommandBlocks([
    block(commandText(), {}), block(`${START}\n{bad json}\n${END}`, {}),
  ]).error, 'ambiguous_command_blocks');
});
test('malformed delimiters, JSON, and unsupported operations are rejected', () => {
  for (const text of [
    `${START}\n{"operation":"create_test_folder","name":"ArenaSmoke"}`,
    `${START}\n${START}\n{}\n${END}`,
    `${START}\n{not json}\n${END}`,
    `${START}\n{"operation":"execute_luau","name":"X"}\n${END}`,
    `${START}\n{"operation":"create_test_folder","name":"X","code":"game:Destroy()"}\n${END}`,
  ]) assert.equal(parseCommandBlock(text).kind, 'invalid');
});
test('hidden duplicate DOM copies are ignored; visible unrelated code is harmless', () => {
  const text = commandText();
  const parsed = parseVisibleCommandBlocks([
    block(text, {}, true),
    block(text, {}, false),
    block('text', {}, true),
  ]);
  assert.equal(parsed.kind, 'command');
  assert.equal(parsed.stats.visibleCodeBlocks, 2);
});
test('repeated observer callbacks claim a response turn only once', () => {
  const handled = new WeakSet();
  const assistantTurn = {};
  assert.equal(claimOnce(handled, assistantTurn), true);
  assert.equal(claimOnce(handled, assistantTurn), false);
});
test('render signature changes when code-block count, visibility, or content changes', () => {
  const one = [{ text: commandText(), visible: true }];
  const two = [...one, { text: commandText(), visible: true }];
  const hidden = [{ text: commandText(), visible: false }];
  assert.equal(renderedResponseSignature('reply', one), renderedResponseSignature('reply', one));
  assert.notEqual(renderedResponseSignature('reply', one), renderedResponseSignature('reply', two));
  assert.notEqual(renderedResponseSignature('reply', one), renderedResponseSignature('reply', hidden));
  assert.notEqual(renderedResponseSignature('reply', one), renderedResponseSignature('changed', one));
});
test('unsafe names and oversized payloads are rejected', () => {
  for (const name of ['../folder', ' leading', 'name/other', 'x'.repeat(41), '']) {
    assert.equal(parseCommandBlock(commandText(name)).kind, 'invalid');
  }
  assert.equal(parseCommandBlock('x'.repeat(200_001)).error, 'response_too_large');
  assert.equal(parseCommandBlock(`${START}\n${'x'.repeat(1025)}\n${END}`).error, 'command_too_large');
});
