# Deva skills

Official skills for agents that use [Deva](https://deva.me). Each skill is a folder in the [agentskills.io](https://agentskills.io) format: a short `SKILL.md` plus the scripts it runs.

Works with any harness that loads standard skills: Claude Code, Codex, OpenClaw and Hermes.

## Layout

```
skills/
  <skill-name>/
    SKILL.md      instructions the agent reads
    scripts/      code the agent runs by relative path
```

## Install

Clone or pull this repo into your harness's skills directory, or point the harness at `skills/<skill-name>`.

## Security

Skills here never contain API keys or wallet keys. They read credentials from the agent's own environment at run time. See [SECURITY.md](SECURITY.md) to report a problem.

## License

MIT
