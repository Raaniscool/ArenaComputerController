'use strict';
(function exposeParser(root) {
  const START = 'BEGIN_ARENA_ROBLOX_COMMAND';
  const END = 'END_ARENA_ROBLOX_COMMAND';
  const MAX_TEXT = 200_000;
  const MAX_BLOCK = 1_024;
  const MAX_NAME = 40;
  const MATERIALS = Object.freeze(['Plastic', 'SmoothPlastic', 'Wood', 'WoodPlanks', 'Concrete', 'Brick', 'Metal', 'Grass', 'Ice', 'Glass', 'Neon', 'Sand', 'Slate', 'Marble']);
  function validName(name) {
    return typeof name === 'string' && name.length >= 1 && name.length <= MAX_NAME
      && name.trim() === name && /^[A-Za-z0-9 _-]+$/.test(name);
  }
  function vector(value, min, max, integer = false) {
    return !!value && typeof value === 'object' && !Array.isArray(value)
      && Object.keys(value).sort().join(',') === 'x,y,z'
      && ['x', 'y', 'z'].every(k => Number.isFinite(value[k]) && value[k] >= min && value[k] <= max && (!integer || Number.isInteger(value[k])));
  }
  function validateCommand(value) {
    if (!value || typeof value !== 'object' || Array.isArray(value) || !validName(value.name)) return null;
    if (value.operation === 'create_test_folder') {
      if (Object.keys(value).sort().join(',') !== 'name,operation') return null;
      return { operation: value.operation, name: value.name };
    }
    if (value.operation !== 'create_part' || Object.keys(value).sort().join(',') !== 'anchored,canCollide,color,material,name,operation,position,size') return null;
    if (!vector(value.position, -10000, 10000) || !vector(value.size, 0.1, 512)
      || !vector(value.color, 0, 255, true) || !MATERIALS.includes(value.material)
      || typeof value.anchored !== 'boolean' || typeof value.canCollide !== 'boolean') return null;
    return { operation: value.operation, name: value.name, position: { ...value.position }, size: { ...value.size }, color: { ...value.color }, material: value.material, anchored: value.anchored, canCollide: value.canCollide };
  }
  function parseCommandBlock(text) {
    if (typeof text !== 'string' || text.length > MAX_TEXT) return { kind: 'invalid', error: 'response_too_large' };
    const startAt = text.indexOf(START);
    const endAt = text.indexOf(END);
    if (startAt === -1 && endAt === -1) return { kind: 'none' };
    if (startAt === -1 || endAt === -1 || text.indexOf(START, startAt + START.length) !== -1
      || text.indexOf(END, endAt + END.length) !== -1 || endAt <= startAt + START.length) {
      return { kind: 'invalid', error: 'invalid_delimiters' };
    }
    const raw = text.slice(startAt + START.length, endAt).trim();
    if (raw.length > MAX_BLOCK) return { kind: 'invalid', error: 'command_too_large' };
    let parsed;
    try { parsed = JSON.parse(raw); }
    catch { return { kind: 'invalid', error: 'invalid_json' }; }
    const command = validateCommand(parsed);
    if (!command) return { kind: 'invalid', error: 'operation_not_allowed' };
    return { kind: 'command', command };
  }
  const elementOf = (block) => block && block.element && (typeof block.element === 'object' || typeof block.element === 'function')
    ? block.element : null;
  const sameCommand = (a, b) => JSON.stringify(a) === JSON.stringify(b);
  function nested(a, b) {
    const ae = elementOf(a), be = elementOf(b);
    if (!ae || !be || ae === be) return false;
    try { return typeof ae.contains === 'function' && ae.contains(be); }
    catch { return false; }
  }
  function result(kind, error, command, stats) {
    return { kind, ...(error ? { error } : {}), ...(command ? { command } : {}), stats };
  }
  function parseVisibleCommandBlocks(blocks) {
    if (!Array.isArray(blocks)) return result('invalid', 'invalid_blocks', null, {
      visibleCodeBlocks: 0, commandBlocks: 0, duplicateNodeReferences: 0, nestedEquivalentCopies: 0,
    });
    const stats = { visibleCodeBlocks: 0, commandBlocks: 0, duplicateNodeReferences: 0, nestedEquivalentCopies: 0 };
    const seenElements = new Set();
    const visible = [];
    for (const block of blocks) {
      if (!block || block.visible !== true) continue;
      const element = elementOf(block);
      if (element && seenElements.has(element)) {
        stats.duplicateNodeReferences++;
        continue;
      }
      if (element) seenElements.add(element);
      visible.push(block);
    }
    stats.visibleCodeBlocks = visible.length;
    const parsed = visible.map(block => ({ block, parsed: parseCommandBlock(block.text) }))
      .filter(entry => entry.parsed.kind !== 'none');
    stats.commandBlocks = parsed.length;
    if (!parsed.length) return result('none', null, null, stats);

    // Some renderers nest a code element inside its visible pre, causing both
    // ancestors to expose the same marker text. Collapse only nested elements
    // whose parsed typed command is identical; never collapse separate blocks.
    const aliases = new Set();
    for (let i = 0; i < parsed.length; i++) {
      for (let j = i + 1; j < parsed.length; j++) {
        if (!nested(parsed[i].block, parsed[j].block) && !nested(parsed[j].block, parsed[i].block)) continue;
        const a = parsed[i].parsed, b = parsed[j].parsed;
        if (a.kind === 'command' && b.kind === 'command' && sameCommand(a.command, b.command)) {
          aliases.add(j);
          stats.nestedEquivalentCopies++;
        } else {
          return result('invalid', 'nested_ambiguous_blocks', null, stats);
        }
      }
    }
    const effective = parsed.filter((_, index) => !aliases.has(index));
    const invalid = effective.filter(entry => entry.parsed.kind !== 'command');
    const commands = effective.filter(entry => entry.parsed.kind === 'command');
    if (invalid.length && commands.length) return result('invalid', 'ambiguous_command_blocks', null, stats);
    if (invalid.length) return result('invalid', invalid[0].parsed.error, null, stats);
    if (commands.length > 1) return result('invalid', 'multiple_command_blocks', null, stats);
    return result('command', null, commands[0].parsed.command, stats);
  }
  function renderedResponseSignature(prose, blocks) {
    if (!Array.isArray(blocks)) return '';
    return JSON.stringify([
      typeof prose === 'string' ? prose : '',
      blocks.map(block => [block && block.visible === true, block && typeof block.text === 'string' ? block.text : '']),
    ]);
  }
  function claimOnce(seen, key) {
    if (!seen || typeof seen.has !== 'function' || typeof seen.add !== 'function' || !key) return false;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  }
  const api = Object.freeze({
    START, END, MAX_TEXT, MAX_BLOCK, MAX_NAME, MATERIALS, validateCommand, parseCommandBlock,
    parseVisibleCommandBlocks, renderedResponseSignature, claimOnce,
  });
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.ArenaCommandParser = api;
})(typeof globalThis === 'undefined' ? this : globalThis);
