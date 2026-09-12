import { describe, it, expect } from 'vitest';
import { pickRunnableModels } from './chatModels';
import type { ModelInfo } from '@/api/types';

const m = (name: string, state: ModelInfo['state'], health: ModelInfo['health']): ModelInfo =>
  ({
    name, group: null, engine: 'vllm', variant: null, port: 1, aliases: [],
    state, health, rates: null, api_key_masked: null, pid: null, log_path: null, vision: null,
  });

describe('pickRunnableModels', () => {
  it('只保留 running 且 healthy（对齐后端 _model_summary 口径）', () => {
    const out = pickRunnableModels([
      m('a', 'running', 'healthy'),
      m('b', 'stopped', null),
      m('c', 'running', 'unknown'),
    ]);
    expect(out.map((x) => x.name)).toEqual(['a']);
  });
});
