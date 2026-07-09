# TBH Companion

Leitor de memória do `TaskBarHero.exe` (somente leitura, IL2CPP + `ReadProcessMemory`)
usado pelo [TBH Tracker](https://www.tbhtracker.online). Detecta início/fim de stage
via os logs oficiais do jogo (`StageClearLog`/`StageFailedLog`), lê tempo oficial,
kills, DPS e ouro, e envia pro backend do site pra aparecer em tempo real na aba
[Stage Tracker](https://www.tbhtracker.online/stage).

Esse repositório existe separado do site principal só pra permitir build público
(GitHub Actions), assinatura de código gratuita via [SignPath Foundation](https://signpath.org/)
e auditoria do código por qualquer pessoa — o companion lê memória de processo, e
achamos importante que isso seja verificável.

## Segurança

- **Somente leitura.** Usa só `ReadProcessMemory` — nunca escreve no processo do
  jogo, não injeta código, não usa hooks, não interfere no anti-cheat.
- **Sem credencial embutida.** O companion não guarda nenhuma credencial do
  Firebase. Ele se vincula à sua conta via um código de pareamento de 6 dígitos
  gerado ao abrir — você confirma logado no site, e o companion recebe um token
  de uso exclusivo pra escrever só nos seus próprios dados.
- **Falso positivo de antivírus é comum** em qualquer app que lê memória de
  processo (é o mesmo padrão usado por ferramentas de overlay/tracker de outros
  jogos). Não temos como eliminar isso sem assinatura de código — código aberto
  aqui é a alternativa: audite você mesmo.

## Arquitetura

- `tbh_companion.py` — código principal (leitura de memória, detecção de stage,
  pareamento com o backend, tray icon, splash e tela de configurações).
- `calib_seed.json` — sementes de calibração (índices/offsets IL2CPP conhecidos).
  **Auto-calibração por versão:** a cada atualização do jogo, o companion
  reencontra as referências de memória sozinho (classes achadas por nome,
  ofuscadas por comportamento; offsets revalidados) e cacheia o resultado em
  `%LOCALAPPDATA%\TBHTracker\`.

## Rodar em dev

```bash
pip install -r requirements.txt
python tbh_companion.py --console --hz 2      # modo console, sem backend
python tbh_companion.py                       # modo normal (splash + pareamento)
```

## Build do executável

```bash
python -m PyInstaller --noconfirm TBHTracker.spec
# saída: dist/TBHTracker.exe
```

## Releases

Builds oficiais saem automaticamente via GitHub Actions a cada tag `v*` (ex.:
`v1.2.0`), publicadas em [Releases](../../releases). O site baixa sempre a
versão mais recente via
`https://github.com/nathanrdn1/tbh-companion/releases/latest/download/TBHTracker.exe`.
