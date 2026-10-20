// The editor was called Abaqus No-Code: copy what this browser saved under the old key names (graphs, tabs,
// library, layout, settings) to the new ones once, before anything reads them. Imported first in main.tsx.
const OLD = "abaqus-nocode.";
const NEW = "nodecode-fea.";

try {
  const old: string[] = [];
  for (let i = 0; i < localStorage.length; i++) {
    const key = localStorage.key(i);
    if (key?.startsWith(OLD)) old.push(key);
  }
  for (const key of old) {
    const renamed = NEW + key.slice(OLD.length);
    if (localStorage.getItem(renamed) === null) localStorage.setItem(renamed, localStorage.getItem(key)!);
  }
} catch {
  // storage blocked or full: the editor starts as on a first visit
}

export {};
