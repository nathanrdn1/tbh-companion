# Contexto pra quem for mexer no TBH Companion

Esse arquivo existe pra você (ou o Claude Code que estiver te ajudando) entender
rápido o que mudou aqui recentemente, antes de continuar desenvolvendo. A
autenticação/distribuição do companion foi **redesenhada do zero** em cima do
PR original (`feat/companion` no repo `tbh-tracker-nuxt`) — se você estava
trabalhando a partir daquele PR, vale ler isso inteiro antes de continuar.

## O que mudou e por quê

### 1. Login anônimo → pareamento por código

**Antes:** o companion recebia `uid`/`db_url`/`api_key` embutidos no final do
`.exe` no momento do download (o site injetava esses bytes via JS), e fazia
login anônimo direto no Firebase Auth pra escrever no RTDB.

**Problemas encontrados:**
- O `_signin()` não tinha backoff — quando falhava, tentava de novo a cada
  tick (~2s), o que estourou o rate-limit de criação de conta anônima do
  Google (`TOO_MANY_ATTEMPTS_TRY_LATER`) durante os testes.
- O token de auth usado pra escrever (da conta anônima) não tinha relação
  nenhuma com o `uid` do caminho (`users/{uid}/...`) sendo escrito — a
  segurança real dependia inteiramente das regras do RTDB permitirem
  qualquer autenticado (mesmo anônimo) escrever em qualquer `uid`.

**Agora:** o companion não guarda nenhuma credencial do Firebase.
1. Ao abrir sem sessão salva, ele chama `POST /api/companion/pair/start` no
   site → recebe um código numérico de 6 dígitos.
2. Copia o código pra área de transferência (ctypes puro, Win32) e abre o
   navegador direto em `{site}/companion-link?code=XXXXXX`.
3. O usuário confirma logado no site (mesma conta da web). A página chama
   `POST /api/companion/pair/confirm` com `{ code, idToken }`.
4. O backend verifica o `idToken` manualmente (RS256 contra os certs
   públicos do Google — **sem** Firebase Admin SDK, sem service account) e
   gera um **token opaco** vinculado ao `uid`, salvo em `companion_tokens/{token}`.
5. O companion recebe esse token via polling (`GET /api/companion/pair/status`)
   e passa a escrever via `POST /api/companion/write` — o servidor resolve o
   `uid` a partir do token (usando o database secret, sem depender das regras
   do RTDB pro companion).

Toda essa lógica do backend (as 4 rotas + verificação de token) vive no repo
**privado** `tbh-tracker-nuxt`, em `server/api/companion/` e
`server/utils/{rtdb,verifyIdToken,verifyAdmin}.ts`. Esse repo aqui
(`tbh-companion`) só tem o lado cliente (Python).

### 2. Companion exclusivo pra assinantes

Não existe sistema de planos ainda — é um flag manual `users/{uid}/subscriber: true`
no Firebase, controlado em `/admin/subscribers` (no site). A checagem acontece
**duas vezes no servidor** (não só na UI):
- No `pair/confirm` — sem o flag, nem gera o token.
- Em **toda escrita** (`write.post.ts`) — revogar o flag corta o companion de
  quem já tinha vinculado antes, não só impede vincular de novo.

### 3. Auto-cura do LogManager + detecção por tail-ptr

Isso foi portado de um PR posterior do Matheus (`03861f8` em `feat/companion`)
que resolvia dois bugs reais de leitura de memória:
- LogManager podia falhar em resolver no attach (menu vazio, sem logs ainda)
  e ficava **travado a sessão toda** sem detecção oficial de clear/fail.
  Agora `_retry_logmanager_if_needed()` reten­ta a cada ~5s até funcionar.
- Detecção de clear/fail trocada de "`_version` da lista" pra "ponteiro da
  última entrada" (`_log_tail_ptr`) — o `_version` também mudava ao abrir
  baú, gerando falso "fail".

### 4. Repo separado + build público (você está aqui)

O companion foi extraído do monorepo privado pra esse repo público por dois
motivos:
- **SignPath Foundation** assina código de graça pra projetos open-source,
  mas exige repo público + licença OSI (MIT aqui).
- **Distribuir via GitHub Releases** (não do domínio do site) evita o aviso
  de "arquivo suspeito" do Chrome — o Google Safe Browsing pesa muito a
  reputação do domínio que serve o binário, e `github.com` tem reputação
  altíssima. Um `.exe` nosso servido de domínio próprio, com hash novo a cada
  build, começa sempre do zero nesse quesito. Confirmamos comparando com o
  [tbh-meter](https://github.com/mad-labs-org/tbh-meter): eles também não são
  assinados, mas distribuem via Releases, e não tomam o aviso do Chrome (só o
  do SmartScreen ao *executar*, que é outro checkpoint, esse sim resolvido só
  com assinatura de código).

`.github/workflows/release.yml` builda com PyInstaller e publica automático
como Release a cada tag `v*` (`git tag vX.Y.Z && git push origin vX.Y.Z`).

**O nome do asset é sempre `TBHTracker.exe`, sem versão no nome** — de
propósito, pra `/releases/latest/download/TBHTracker.exe` funcionar direto
sem precisar de chamada de API pra descobrir o nome do arquivo primeiro.

### 5. Auto-atualização

Ao abrir (antes até de checar pareamento), o companion consulta
`GET /repos/nathanrdn1/tbh-companion/releases/latest` e compara com
`COMPANION_VERSION`. Se tiver versão nova:
1. Baixa o novo `.exe` pra `%TEMP%` (com checagem mínima de sanidade: tamanho
   + header PE `MZ`).
2. Sobe o novo exe em modo especial (`--finish-update <path_original> <pid_antigo>`).
3. O processo atual fecha. O novo exe espera o PID antigo morrer (polling via
   `OpenProcess`), se copia por cima do exe original, relança a partir de lá,
   e sai.

Isso já foi testado de ponta a ponta (publicando uma versão de teste e
observando uma cópia antiga se atualizar sozinha) — funciona.

**Importante:** o backend do site (`app/pages/stage.vue`) também checa a
versão mais recente direto no GitHub Releases pro banner de "nova versão
disponível" — não tem mais nenhuma versão hardcoded no site pra sincronizar
manualmente a cada release.

## Coisas importantes pra não quebrar

1. **`BACKEND_BASE` (topo do arquivo) tem que ficar em
   `https://www.tbhtracker.online`** em qualquer coisa commitada/publicada.
   Só aponte pra localhost/preview em testes locais, e **nunca commite**
   assim.
2. **`VERCEL_BYPASS_SECRET` tem que ficar vazio (`""`) em qualquer commit.**
   Esse valor só existe pra testar contra previews protegidos por SSO da
   Vercel — se um valor real for commitado e acabar num `.exe` publicado,
   qualquer pessoa que baixar o `.exe` consegue extrair o secret e pular a
   proteção de qualquer preview do projeto.
3. **Pra lançar uma versão nova:** bump `COMPANION_VERSION` no topo do
   arquivo → `git tag vX.Y.Z` → `git push origin main && git push origin vX.Y.Z`.
   O CI builda e publica sozinho.
4. Esse repo tem uma **cópia espelhada** em `companion/tbh_companion.py` no
   monorepo privado (`tbh-tracker-nuxt`, branch `dev`/`main`) — mantida em
   sincronia manual por enquanto (não é build a partir de lá). Se puder,
   avise quando fizer mudanças aqui pra manter as duas em dia, ou a gente
   formaliza um jeito automático depois.
5. **PR #1** (`feat/companion` → `tbh-tracker-nuxt`) deveria estar fechado —
   ficou baseado na arquitetura antiga (login anônimo). As melhorias de
   leitura de memória de lá (item 3 acima) já foram portadas manualmente.

## O que ainda falta (não fizemos)

- **Assinatura de código real** (SignPath Foundation, grátis, mas exige
  aprovação deles — ainda não aplicamos formalmente) ou paga (resolve o
  aviso do SmartScreen ao executar, que ainda aparece).
- **Instalador de verdade** (atalho na Área de Trabalho/Menu Iniciar,
  desinstalador) — hoje é só o `.exe` avulso. "Iniciar com o Windows" já
  existe (tela de Configurações, via registro), mas não tem instalador.
- Log viewer da tela de Configurações foi removido a pedido (achamos mais
  debug do que útil pro usuário final) — se precisar de novo pra
  diagnosticar algo, ele existia e foi tirado de propósito, não é bug.

## Onde fica o quê

| O quê | Onde |
|---|---|
| Companion (Python) | aqui, `tbh_companion.py` |
| Rotas de pareamento/escrita | `tbh-tracker-nuxt`: `server/api/companion/` |
| Verificação de idToken/admin | `tbh-tracker-nuxt`: `server/utils/{verifyIdToken,verifyAdmin,rtdb}.ts` |
| Página de vínculo | `tbh-tracker-nuxt`: `app/pages/companion-link.vue` |
| Stage Tracker (site) | `tbh-tracker-nuxt`: `app/pages/stage.vue`, `app/stores/stage.js` |
| Admin de assinantes | `tbh-tracker-nuxt`: `app/pages/admin/subscribers.vue`, `server/api/admin/subscribers.*` |
| Build/release do companion | aqui, `.github/workflows/release.yml` |
