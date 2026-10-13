# SAP Gateway — API REST genérica sobre serviços SOAP

Camada intermediária (FastAPI) entre consumidores REST e serviços SOAP corporativos, em especial SAP.
Recebe HTTP Basic, valida a entrada, executa a operação SOAP com **as credenciais do próprio usuário**
e devolve uma resposta REST consistente — com observabilidade completa (OpenTelemetry → Grafana Alloy →
Tempo / Loki / Prometheus → Grafana).

> **Sobre o contrato SOAP incluído.** Nenhum WSDL real foi fornecido, então o repositório traz um contrato
> **representativo** (`MaintenanceNotification`, estilo SAP PM: ler / pesquisar / criar notas de manutenção)
> em `src/sap_gateway/wsdl/`, servido por um SAP simulado (`mock-sap`). A infraestrutura é genérica; para
> integrar um SAP real, substitua/adicione WSDL, contratos e mapeamentos conforme
> [Adicionando um novo serviço SOAP](#adicionando-um-novo-serviço-soap). **Nada aqui foi testado contra um
> SAP real.**

## Sumário

1. [Arquitetura](#arquitetura)
2. [Tecnologias e versões](#tecnologias-e-versões)
3. [Pré-requisitos e instalação](#pré-requisitos-e-instalação)
4. [Início rápido](#início-rápido)
5. [Configuração (variáveis de ambiente)](#configuração-variáveis-de-ambiente)
6. [Exemplos de requisições](#exemplos-de-requisições)
7. [Autenticação e isolamento de credenciais](#autenticação-e-isolamento-de-credenciais)
8. [Serviços SOAP: configuração, segurança de destino e cache de WSDL](#serviços-soap-configuração-segurança-de-destino-e-cache-de-wsdl)
9. [Contratos e mapeamentos](#contratos-e-mapeamentos)
10. [Adicionando um novo serviço SOAP](#adicionando-um-novo-serviço-soap)
11. [Erros, timeouts e retries](#erros-timeouts-e-retries)
12. [Observabilidade](#observabilidade)
13. [Ambiente local com Docker Compose](#ambiente-local-com-docker-compose)
14. [Consultas de exemplo e investigação](#consultas-de-exemplo-e-investigação)
15. [Testes, Ruff e qualidade](#testes-ruff-e-qualidade)
16. [Privacidade e retenção](#privacidade-e-retenção)
17. [Validação realizada e limitações conhecidas](#validação-realizada-e-limitações-conhecidas)

---

## Arquitetura

```
Consumidor ──HTTP Basic──► FastAPI ──► Serviço (orquestração) ──► SoapGateway ──► SAP (SOAP)
                            │  rota        │  mapeadores            │  Zeep + requests
                            │  schemas     │  REST ⇄ contrato SOAP  │  sessão/cliente POR TENTATIVA
                            ▼  (Pydantic)  ▼  (Pydantic)            ▼
                     erros tipados → resposta JSON estruturada   WSDL parseado 1x (somente leitura)

Telemetria:  API ──OTLP gRPC (4317)──► Grafana Alloy ──┬─ traces ─► Tempo
                                                      ├─ logs   ─► Loki        (OTLP nativo)
                                                      └─ métricas► Prometheus  (remote write)
                                          Grafana consulta os três; logs ⇄ traces correlacionados
```

Fluxo de uma requisição: middleware (contexto + log de acesso) → dependência de autenticação (extrai Basic) →
validação Pydantic → serviço → mapeador REST→SOAP → `SoapGateway.call` (span `SOAP serviço/operação`, retries,
métricas) → Zeep → SAP → resposta lida pelo Zeep, convertida *preservando ausente/nil/vazio* → contrato SOAP
(Pydantic) → mapeador SOAP→REST → resposta. Qualquer falha vira um `AppError` tipado e depois JSON padronizado.

```
src/sap_gateway/
├── main.py                 fábrica `create_app()` e entrypoint `sap-gateway`
├── config.py               Pydantic Settings (APP_*, OTEL_*) — nenhum segredo com valor padrão
├── auth.py                 Basic Auth no limite da API; `BasicCredentials` imutável (senha fora de repr)
├── context.py              contextvars de diagnóstico (usuário, rota, operação) — NUNCA senha
├── errors.py               hierarquia `AppError` + handlers → JSON `{"error": {...}}`
├── routes/health.py        /health/live e /health/ready
├── soap/                   infraestrutura reutilizável (independe do serviço)
│   ├── contracts.py        `SoapModel` (Pydantic ↔ Zeep), `SoapServiceSpec`, `SoapOperation`
│   ├── gateway.py          `SoapGateway.call(...)`: sessão por tentativa, retry, tradução de erros, spans/métricas
│   ├── wsdl.py             `WsdlStore`: carga única e cache do WSDL/XSD (sem credenciais de usuário)
│   ├── transport.py        `LoadTransport` (anti-SSRF) e `RequestTransport` (por tentativa)
│   ├── plain.py            Zeep → estrutura simples preservando ausente × nil × vazio
│   └── retry.py            política de retry (idempotência) e backoff com jitter
├── services/maintenance_notification/   um serviço publicado
│   ├── contracts.py        modelos SOAP + operações (nomes XML, idempotência, fault)
│   ├── schemas.py          modelos REST públicos
│   ├── mappers.py          mapeamentos explícitos REST ⇄ SOAP
│   ├── service.py          orquestração + tradução de faults de domínio
│   └── router.py           rotas FastAPI (contrato, docs, códigos HTTP)
├── observability/
│   ├── telemetry.py        inicialização única: providers, exporters OTLP, instrumentações, shutdown
│   ├── logging.py          JSON no stdout, filtros de contexto/sanitização, handler OTLP
│   ├── sanitize.py         redação de segredos
│   ├── metrics.py          instrumentos `soap.client.*` e `app.health.status`
│   └── middleware.py       contexto da requisição + log de acesso
├── devtools/mock_sap.py    SAP simulado (dev/testes) — NÃO é código de produção
└── wsdl/                   WSDL/XSD de exemplo
observability/              Alloy, Tempo, Loki, Prometheus, Grafana (datasources + 4 dashboards)
compose.yaml  Dockerfile    ambiente local e imagem
tests/                      unit/ integration/ e2e/ (+ receptores OTLP de teste)
```

## Tecnologias e versões

Versões verificadas em conjunto (o `uv` resolveu sem conflitos; `uv.lock` versionado). OpenTelemetry fixado
em pares compatíveis (`1.45.1` SDK/API/exporters ↔ `0.66b1` instrumentações).

| Componente | Versão | Observação |
|---|---|---|
| Python | 3.13 | `requires-python = ">=3.13,<3.14"` |
| FastAPI / Pydantic / pydantic-settings | 0.143 / 2.14 / 2.15 | |
| Zeep / requests / lxml | 4.3.3 / 2.34 / 6.1 | |
| uvicorn | 0.54 | |
| OpenTelemetry SDK + exporters OTLP (gRPC e HTTP) | 1.45.1 | |
| instrumentation-fastapi / -requests / -logging | 0.66b1 | `LoggingHandler` oficial (o do SDK está *deprecated*) |
| pytest / pytest-timeout / httpx2 / Ruff / mypy | 9.1 / 2.4 / 2.13 / 0.17 / 2.4 | `httpx2` é o cliente que o `TestClient` do Starlette agora exige |
| Grafana Alloy / Tempo / Loki / Prometheus / Grafana | 1.20.1 / 2.9.0 / 3.7.8 / 3.15.0 / 13.2.3 | imagens fixadas no `compose.yaml` |

## Pré-requisitos e instalação

* Python 3.13 e [`uv`](https://docs.astral.sh/uv/) (para a API, testes e Ruff);
* Docker + Docker Compose v2 (somente para o ambiente de observabilidade).

```bash
uv sync                 # cria .venv e instala dependências (inclui o grupo dev)
cp .env.example .env    # opcional; todos os valores são fictícios
```

## Início rápido

```bash
# 1) SAP simulado (terminal 1) — defina usuários de demonstração
MOCK_SAP_USERS="alice:troque-me,bob:troque-me-tambem" uv run mock-sap     # :8081

# 2) API (terminal 2) — sem telemetria exportada, não precisa de Grafana/Alloy
APP_SOAP_SERVICES__MAINTENANCE_NOTIFICATION__ENDPOINT_URL=http://127.0.0.1:8081/soap/maintenance-notification \
  uv run sap-gateway                                                       # :8000

# 3) Chamada (docs interativas em http://127.0.0.1:8000/docs)
curl -u alice:troque-me http://127.0.0.1:8000/api/v1/notifications/000000000002
```

Com `cp .env.example .env` os passos 1 e 2 reduzem-se a `uv run mock-sap` e `uv run sap-gateway`
(o `.env.example` já habilita a exportação OTLP: suba antes a stack da seção [Compose](#ambiente-local-com-docker-compose)
ou defina `APP_OTEL_*_ENABLED=false`). Para recarregamento automático em desenvolvimento:
`uv run uvicorn sap_gateway.main:create_app --factory --reload` (nesse modo o uvicorn imprime os próprios logs em texto).

**IDs “mágicos” do mock** (para demonstrar falhas): `000000000001` (mistura ausente/`nil`/vazio),
`000000000002` (completo), `…404` (SOAP Fault de domínio → HTTP 404), `…403`, `…500`/`…503` (erro HTTP),
`…504` (lento → timeout), `…999` (XML inválido), `…998` (elemento inesperado), `…997` (código fora do enum),
plantas `9999` (coleção vazia) e `9998` (coleção ausente) na pesquisa; descrição `FAIL_INVALID` na criação.

## Configuração (variáveis de ambiente)

Centralizada em `config.py`; `.env.example` documenta tudo. **Nenhuma variável é obrigatória para subir a API**
(os padrões servem ao desenvolvimento local). Em `staging`/`production` o endpoint SOAP precisa ser **https**
(do contrário a chamada é recusada com 503 antes de enviar qualquer credencial).

| Variável | Padrão | Obrig.? | Descrição |
|---|---|---|---|
| `APP_ENVIRONMENT` | `local` | recomendada | `local` \| `test` \| `staging` \| `production` (vira `deployment.environment`) |
| `APP_HOST` / `APP_PORT` | `127.0.0.1` / `8000` | não | bind da API |
| `APP_LOG_LEVEL` | `INFO` | não | nível do logging |
| `OTEL_SERVICE_NAME` (ou `APP_SERVICE_NAME`) | `sap-gateway` | não | `service.name` |
| `APP_SERVICE_VERSION` | versão do pacote | não | `service.version` |
| `APP_SOAP_SERVICES__<KEY>__ENDPOINT_URL` | `soap:address` do WSDL | em ambientes reais | destino SOAP (override por ambiente) |
| `…__WSDL` | WSDL empacotado | não | caminho local ou URL http(s) |
| `…__AUTH_MODE` | `http_basic` | não | `http_basic` \| `ws_security_username_token` \| `none` |
| `…__CONNECT_TIMEOUT_SECONDS` / `…__READ_TIMEOUT_SECONDS` | `5` / `30` | não | timeouts explícitos |
| `…__WSDL_TIMEOUT_SECONDS` | `15` | não | timeout de carga de WSDL/XSD |
| `…__RETRY__MAX_ATTEMPTS` / `…BACKOFF_BASE_SECONDS` / `…BACKOFF_MAX_SECONDS` | `3` / `0.2` / `2` | não | retry com backoff exponencial + jitter |
| `…__TLS__VERIFY` / `…CA_BUNDLE` / `…CLIENT_CERT` / `…CLIENT_KEY` | `true` / – | não | TLS (verificação sempre ligada por padrão) |
| `…__PROXY_URL` | – | não | proxy explícito (`HTTP(S)_PROXY` do ambiente é ignorado) |
| `…__ALLOWED_HOSTS` | – | não | hosts extras para imports de WSDL/XSD (csv) |
| `…__WSDL_USERNAME` / `…__WSDL_PASSWORD` | – | não | credencial **técnica** só para baixar WSDL protegido |
| `APP_OTEL_TRACES_ENABLED` / `…METRICS…` / `…LOGS…` | `false` | não | liga cada sinal OTLP (padrão desligado: API sobe sem backends) |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `grpc` | não | `grpc` (4317, preferido) \| `http/protobuf` (4318) — o exporter segue o protocolo |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4317` | não | endpoint do Alloy (para HTTP o sufixo `/v1/<sinal>` é adicionado) |
| `OTEL_EXPORTER_OTLP_TIMEOUT` | `5000` ms | não | timeout de cada exportação |
| `OTEL_EXPORTER_OTLP_HEADERS` | – | não | `k=v,k2=v2` (segredo) |
| `OTEL_METRIC_EXPORT_INTERVAL` | `15000` ms | não | intervalo das métricas |
| `APP_OTEL_BATCH_SCHEDULE_DELAY_MS` / `APP_OTEL_SHUTDOWN_TIMEOUT_MS` | `2000` / `3000` | não | lote de spans/logs |
| `APP_OTEL_HTTP_METRICS_ENABLED` / `APP_OTEL_HTTP_EXCLUDED_URLS` | `true` / probes+docs | não | métricas/spans HTTP e rotas ignoradas (sondas de saúde não geram telemetria) |

`<KEY>` é a chave do serviço (`MAINTENANCE_NOTIFICATION`); hierarquia aninhada com `__`. Segredos são `SecretStr`
(não aparecem em `repr`). Não há senha nem token em arquivos versionados.

## Exemplos de requisições

```http
GET /api/v1/notifications/000000000001
Authorization: Basic <base64 usuario:senha>
```
```json
{
  "notification_id": "000000000001",
  "notification_type": "malfunction_report",
  "description": "",
  "plant": "1000",
  "reported_by": null,
  "planned_cost": null,
  "status": "outstanding",
  "created_on": "2026-03-01",
  "created_at": "2026-03-01T10:20:30Z",
  "items": [{ "item_number": 1, "description": "", "damage_code": null }],
  "long_text": [],
  "attributes": []
}
```
Aqui `description: ""` = elemento XML **vazio**; `reported_by: null` = `xsi:nil="true"`; `functional_location`
**omitido** = elemento **ausente** (ver [Contratos e mapeamentos](#contratos-e-mapeamentos)).

```http
POST /api/v1/notifications        → 201, Location: /api/v1/notifications/000000001000
Content-Type: application/json
{"notification_type": "malfunction_report", "description": "Bomba vazando", "plant": "1000",
 "priority": "high", "reported_by": null, "items": [{"item_number": 1, "damage_code": null}]}
```
```json
{ "notification_id": "000000001000", "messages": [{ "severity": "success", "text": "Nota criada com sucesso" }] }
```

`GET /api/v1/notifications?plant=1000&status=in_process&max_results=10` →
`{"notifications": [...], "truncated": false}`.

Erro (formato único para todos os casos; `trace_id` permite achar o trace/logs):
```json
{ "error": { "code": "not_found", "message": "Nota de manutenção não encontrada.",
             "trace_id": "472327dc1efa5025f42424602d9cb0b1" } }
```

## Autenticação e isolamento de credenciais

**Recebimento.** `auth.py` extrai `Authorization: Basic` via dependência FastAPI (documentada no OpenAPI como
`BasicAuth`). A API **não possui base de usuários**: o segredo só pode ser validado pelo SAP. Localmente rejeitamos
cabeçalho ausente/malformado, vazio, com caracteres de controle ou acima de 128/256 caracteres (401 +
`WWW-Authenticate`, sem chamar o SAP). Credenciais rejeitadas pelo SAP (HTTP 401) voltam como 401
`authentication_failed`; 403 como `forbidden`.

**Encaminhamento.** Conforme `AUTH_MODE` do serviço (HTTP Basic **não** equivale a WS-Security):

* `http_basic` (padrão SAP/ICF): `Authorization: Basic` na requisição HTTP ao SOAP;
* `ws_security_username_token`: `wsse:UsernameToken` no cabeçalho SOAP (senha em texto — use apenas com https);
* `none`: sem credenciais.

**Descarte e isolamento.**

1. `BasicCredentials` é um dataclass imutável com `repr`/`str` mascarados e não serializável; trafega **por
   argumentos** (rota → serviço → gateway), nunca por variável global, singleton ou `contextvars`.
2. A cada **tentativa** de chamada criamos `requests.Session` → `RequestTransport` → `zeep.Client` (+ `wsse`) novos;
   a sessão é fechada em `finally`. Cookies do SAP (`SAP_SESSIONID`, `MYSAPSSO2`…) morrem com a sessão. Sem
   `netrc`/proxy herdados do ambiente (`trust_env=False`), sem redirects (nunca reenvia credenciais a outro host).
3. O único objeto compartilhado é o `Document` WSDL parseado, somente-leitura e **sem credenciais de usuário**.
4. `contextvars` guarda só diagnóstico (usuário, método, rota, serviço/operação SOAP).
5. Senhas, `Authorization`, cookies e tokens são redigidos em logs, traces e mensagens de erro; o log de acesso não lê
   cabeçalhos; `zeep.transports` (que loga envelopes em DEBUG, inclusive WS-Security) é fixado em WARNING e o
   transporte próprio não loga corpos.

Testes (`tests/integration/test_credentials_isolation.py`): duas requisições simultâneas com usuários diferentes
(sobreposição **forçada** por barreira no mock) usam cada uma sua credencial, nenhuma envia `Cookie`; a falha de uma
não afeta outra; 120 requisições concorrentes mistas; sessões distintas e fechadas; e a senha não aparece em resposta,
logs, spans nem métricas. Foram feitas *mutações deliberadas* (sessão compartilhada, log da senha, retry indevido)
confirmando que esses testes falham.

## Serviços SOAP: configuração, segurança de destino e cache de WSDL

* **Destinos vêm só da configuração.** Nenhum endpoint recebe nome de serviço/operação/URL do consumidor; não existe
  rota “executa qualquer operação”. Cada operação publicada é uma rota explícita, documentada e tipada.
* **Anti-SSRF na carga de WSDL/XSD.** `LoadTransport` só lê (a) arquivos dentro do diretório do WSDL configurado e
  (b) hosts do WSDL + `ALLOWED_HOSTS`; esquemas `ftp:`, `file:///etc/...`, `../` e IPs de metadata são recusados.
  Chamadas de negócio só podem ser feitas ao endereço resolvido da configuração (`RequestTransport` recusa qualquer
  outro). O `soap:address` do WSDL é tratado como configuração confiável; `ENDPOINT_URL` o sobrescreve.
* **Estratégia de cache.** O WSDL é parseado **uma vez** (lazy, com lock; falhas não são cacheadas, então a API se
  recupera quando o WSDL volta) e compartilhado como objeto somente-leitura. Prefira WSDL local (sem rede nem
  credencial). Se for por URL, usa-se uma credencial *técnica* separada, aplicada por carga em sessão descartável.
* **XML endurecido.** Zeep por padrão não proíbe DTD/entidades; fixamos `forbid_dtd`, `forbid_entities` e
  `forbid_external` (nas chamadas), `strict=True` e `xml_huge_tree=False`.
* **Zeep está bem isolado da camada REST:** schemas públicos não importam Zeep.

## Contratos e mapeamentos

* Cada operação tem **modelos Pydantic explícitos** (`contracts.py`) com nome XML via alias (`alias_generator=to_pascal`
  + `populate_by_name`), `extra="forbid"` (campo desconhecido falha, não é descartado), imutáveis, enums de domínio,
  decimais/datas/`datetime` tipados, wrappers de coleção (`Items/Item`) e `idempotent` declarado.
* **Ausente × `null` × vazio** (não são equivalentes):

  | Conceito | XML | SOAP (modelo) | REST (JSON) |
  |---|---|---|---|
  | ausente | elemento omitido | campo não definido | chave omitida |
  | nulo | `xsi:nil="true"` | campo `None` definido | `null` |
  | vazio | `<X/>` | `""` | `""` |

  Na ida usamos `model_fields_set` (+ `zeep.xsd.Nil`); na volta, `soap/plain.py` percorre em paralelo o objeto Zeep e o XML
  da resposta (o Zeep colapsa os três casos em `None`; ele ainda “desembrulha” respostas de filho único — por isso
  fazemos o parse com `client.get_element(...).parse` e não dependemos do retorno do serviço). Coleções são sempre
  arrays no REST (zero itens = ausente ou vazio). Elemento vazio de tipo não-string (`<Qty/>`) **falha explicitamente**.
* **Mapeadores explícitos** (`mappers.py`): tabelas de códigos bidirecionais com teste de exaustividade (`M1`↔
  `maintenance_request`, …), achatamento do cabeçalho, listas aninhadas; código desconhecido → `contract_mismatch` (502).
* **Limites atuais**: `to_plain` cobre elementos (não atributos XML, `xsd:any`/`anyType`, que falham explicitamente).

## Adicionando um novo serviço SOAP

1. **Contrato**: coloque o WSDL/XSD em `src/sap_gateway/wsdl/` (ou aponte `…__WSDL`).
2. **Pacote** `services/<nome>/` copiando `maintenance_notification/`:
   * `contracts.py` — `SERVICE = SoapServiceSpec(key="<nome>", namespace=..., binding=..., default_wsdl=...)`, modelos
     `SoapModel` por tipo do XSD (alias = nome do elemento; `X | None = None` p/ `minOccurs=0`/`nillable`) e
     `SoapOperation(service=SERVICE, name="MinhaOp", request_model=..., response_model=..., idempotent=True|False,
     fault_model=..., fault_element=...)`. **`idempotent=False` desativa retry após o envio.**
   * `schemas.py` — contratos REST (Pydantic); `mappers.py` — conversões explícitas e `_carry` para não perder o
     tri-estado; `service.py` — orquestração e tradução de faults de domínio (`translate_fault`); `router.py` — rotas
     com `response_model`, `responses`, `CurrentCredentials`.
3. **Registro** em `main.py`: instancie o serviço (`app.state.<x>_service = ...`), `app.include_router(...)` e
   acrescente a chave em `app.state.service_keys` (readiness).
4. **Configuração**: `APP_SOAP_SERVICES__<KEY>__ENDPOINT_URL` etc. (a chave é o `key` do `SoapServiceSpec`).
5. **Testes**: espelhe `tests/` (mapeadores, tri-estado, rotas contra o mock). Nada de infraestrutura é duplicada:
   credenciais, sessão, retry, métricas, spans e logs vêm do `SoapGateway`.

## Erros, timeouts e retries

| Situação | HTTP | `error.code` | `error.type` (telemetria) |
|---|---|---|---|
| Basic ausente/inválido | 401 | `authentication_required` | `authentication` |
| SAP rejeitou usuário/senha | 401 | `authentication_failed` | `authentication` |
| Sem autorização no SAP | 403 | `forbidden` | `authorization` |
| Entrada inválida | 422 | `validation_error` | – |
| Fault de domínio (ex.: nota inexistente) | 404 / 422 | `not_found` / `business_rule_violation` | `not_found` / `business_rule` |
| WSDL/XSD indisponível ou endpoint inseguro | 503 | `soap_contract_unavailable` | `wsdl_unavailable` |
| Conexão recusada/DNS/TLS | 502 | `soap_connection_error` | `connection` |
| Timeout de conexão/leitura | 504 | `soap_timeout` | `timeout` |
| HTTP ≠ 200/SOAP do SAP (4xx/5xx, redirect) | 502 | `soap_transport_error` | `http_status` |
| SOAP Fault não mapeado | 502 | `soap_fault` | `soap_fault` |
| XML malformado / elemento inesperado | 502 | `soap_malformed_response` | `malformed_response` |
| Resposta fora do contrato (enum, tipo) | 502 | `contract_mismatch` | `contract_mismatch` |
| Falha ao serializar a requisição (defeito de mapeamento) | 500 | `soap_serialization_error` | `serialization` |
| Inesperado | 500 | `internal_error` | – |

Mensagens são fixas: nunca contêm faultstring, corpo, URL interna, stack trace ou credenciais (testado).

**Retry** — só falhas transitórias e quando repetir é seguro, com backoff exponencial **full jitter**
(`U(0, min(max, base·2^(n-1)))`) e limite de tentativas:

| Falha | Operação idempotente | Não idempotente (ex.: *criar*) |
|---|---|---|
| conexão recusada / DNS / timeout **de conexão** (nada foi enviado) | retry | retry |
| timeout de **leitura**, erro de conexão após envio, HTTP 502/503/504 | retry | **nunca** |
| SOAP Fault, 4xx, 401/403, XML/contrato inválido, TLS | nunca | nunca |

Retries aparecem como eventos `soap.retry` no span da chamada, spans `soap.attempt` e métricas — **sem logs
próprios** (um único log por chamada). Timeouts de conexão/leitura são explícitos (`requests`).

## Observabilidade

Inicialização única em `observability/telemetry.py` (um `Telemetry` por aplicação, providers próprios, handlers do
`logging` idempotentes e removidos no shutdown). Recursos compartilhados pelos três sinais: `service.name`,
`service.version`, `deployment.environment`, `service.instance.id`.

### Traces
`FastAPIInstrumentor` (span SERVER por rota normalizada, ex.: `GET /api/v1/notifications/{notification_id}`) →
`SOAP <serviço>/<operação>` (atributos `soap.service`, `soap.operation`, `soap.attempts`, `soap.outcome`, `error.type`,
`enduser.id`; eventos `soap.retry`) → `soap.attempt` (um por tentativa) → span CLIENT HTTP da instrumentação do `requests`
(propaga `traceparent`). O usuário está em `enduser.id` no span do servidor e no span SOAP. Não se grava corpo SOAP,
cabeçalhos de autenticação nem mensagens de exceção (`record_exception=False` nos spans próprios).

### Métricas (nomes no Prometheus entre parênteses)
| Instrumento | Tipo / unidade | Atributos |
|---|---|---|
| `http.server.request.duration` (`http_server_request_duration_seconds_*`) — instrumentação oficial, semântica HTTP estável; a contagem é o total de requisições | histograma / s | `http.route` (template), `http.request.method`, `http.response.status_code`, `error.type`(5xx) |
| `soap.client.calls` (`soap_client_calls_total`) — chamada lógica por resultado final | contador | `soap.service`, `soap.operation`, `error.type` (só em falha) |
| `soap.client.duration` (`soap_client_duration_seconds_*`) — inclui retries | histograma / s | idem |
| `soap.client.timeouts` (`soap_client_timeouts_total`) — por tentativa | contador | `soap.service`, `soap.operation` |
| `soap.client.retries` (`soap_client_retries_total`) | contador | `soap.service`, `soap.operation`, `error.type` |
| `app.health.status` (`app_health_status`) | gauge 0/1 | – |

Cardinalidade: **proibido** usuário, ID de nota, trace/requisição em métricas (testado: atributos ⊆ lista permitida).
Uso por usuário vem de logs e traces.

### Logs: JSON local × OTLP
O **mesmo** registro (já sanitizado) segue por dois caminhos independentes:

| | stdout JSON | OTLP (`LoggingHandler` oficial) |
|---|---|---|
| Para quê | local/`docker logs`; funciona sem nenhum backend | Loki via Alloy, com `trace_id`/`span_id` **nativos** do LogRecord |
| Ligado por | sempre | `APP_OTEL_LOGS_ENABLED=true` |
| Campos | `timestamp, severity, message, service.name, deployment.environment, username, trace_id, span_id, http.route, http.request.method, http.response.status_code, duration_ms, soap.service, soap.operation, error.type` (+`exception.*` sanitizados) | os mesmos como atributos do registro |

O Alloy **não** coleta o stdout da API (evita duplicidade). Contexto (`username`, rota, operação SOAP) é adicionado
automaticamente por filtro — não é preciso passá-lo a cada `logger.info`. Falhas dos próprios exporters OTLP não
retornam ao pipeline OTLP (sem laço de realimentação).

### Resiliência da exportação
Exporters rodam em threads de fundo com fila limitada e **timeout** (`OTEL_EXPORTER_OTLP_TIMEOUT`); Alloy fora do ar
descarta telemetria sem bloquear nem derrubar requisições (testado com gRPC e HTTP: latência de requisição < 1 s e
encerramento limitado). Com o coletor indisponível aparecem logs `ERROR` dos exporters (um por lote falho; os avisos de
retry são suprimidos). No shutdown (`SIGTERM`) os providers são esvaziados (flush) e encerrados.

## Ambiente local com Docker Compose

```bash
docker compose up -d                  # Alloy, Tempo, Loki, Prometheus, Grafana (portas só em 127.0.0.1)
# suba mock + API no host (início rápido) com APP_OTEL_*_ENABLED=true, gere tráfego, e abra o Grafana
docker compose --profile app up -d --build   # opcional: mock-sap + API também em containers (MOCK_SAP_USERS no .env)
docker compose down                   # encerra (mantém dados);  down -v  apaga volumes
```

| Serviço | Porta (127.0.0.1) | Função |
|---|---|---|
| Grafana | 3000 (`admin` / `admin` — **só dev**, mude `GRAFANA_ADMIN_PASSWORD`) | consulta e dashboards |
| Alloy | 4317 gRPC, 4318 HTTP (OTLP), 12345 (UI) | receptor e roteador de telemetria |
| Tempo / Loki / Prometheus | 3200 / 3100 / 9090 | armazenamento (consulta direta opcional) |

Portas e senhas são variáveis (`ALLOY_OTLP_GRPC_PORT`, `GRAFANA_PORT`, …). Tempo roda como root no volume nomeado
(somente dev; ver comentário no compose).

### Pipelines (quem produz e encaminha cada sinal)
`observability/alloy/config.alloy`: `otelcol.receiver.otlp` (4317/4318) → `otelcol.processor.batch` (sem transformações
que descartem atributos) →
* **traces** → `otelcol.exporter.otlp` → **Tempo** (`tempo:4317`);
* **logs** → `otelcol.exporter.otlphttp` → **Loki** `/otlp` (ingestão OTLP nativa);
* **métricas** → `otelcol.exporter.prometheus` → `prometheus.remote_write` → **Prometheus** `/api/v1/write`.

Estratégia de métricas: o Prometheus **não** recebe OTLP diretamente; o Alloy converte para o modelo Prometheus e envia por
*remote write* (`--web.enable-remote-write-receiver`). `resource_to_telemetry_conversion` leva `service_name`,
`deployment_environment` etc. a labels (todas de cardinalidade baixa).

**Loki** (`loki.yaml`): `allow_structured_metadata: true`, esquema v13/TSDB e `otlp_config` com
`ignore_defaults: true` — **apenas** `service.name` e `deployment.environment` viram labels indexados; `username`,
`trace_id`, `span_id`, `http.route`, `soap.operation`, `error.type`, `duration_ms`… ficam como *structured metadata*
(pesquisáveis, sem custo de cardinalidade; pontos viram `_`: `http_route`).

**Datasources do Grafana** (`provisioning/datasources`, uids `prometheus`, `loki`, `tempo`):
* Loki → Tempo: `derivedFields` (`matcherType: label`, `matcherRegex: trace_id` → link “Abrir trace”);
* Tempo → Loki: `tracesToLogsV2` com consulta customizada `{service_name=~".+"} | trace_id="${__trace.traceId}"`
  (necessária porque `trace_id` é structured metadata, não texto da linha);

### Dashboards (`observability/grafana/dashboards/*.json`, provisionados, pasta “SAP Gateway”)
Usam variáveis de datasource (`DS_PROM`, `DS_LOKI`, `DS_TEMPO`): também podem ser **importados** em outro Grafana
(*Dashboards → New → Import*), desde que as métricas/logs tenham a mesma forma.

| Dashboard | Conteúdo |
|---|---|
| Visão geral da API | volume, req/s, p50/p95/p99, erros 4xx/5xx, distribuição de status, disponibilidade (`app_health_status`), rotas e operações SOAP mais usadas, consumo ao longo do tempo |
| Integrações SOAP | chamadas por serviço/operação, latência, taxa de falhas, timeouts, retries, erros por categoria, operações mais lentas e com mais falhas |
| Uso por usuário | (Loki + Tempo) usuários que mais usam, rotas e operações SOAP por usuário, erros e latência por usuário, logs e traces (filtro `username` por regex) |
| Investigação de falhas | falhas por período/rota/usuário/operação/tipo de erro, logs de falha com link para trace, traces com erro e lentos |

## Consultas de exemplo e investigação

**Identificar o usuário e o SOAP de uma requisição**

* Resposta de erro traz `trace_id`. Em **Loki**:
  `{service_name="sap-gateway"} | trace_id="<id>"` → todos os logs (acesso `http request completed` + `soap call ...`),
  cada um com `username`, `http_route`, `soap_operation`.
* Em **Tempo**: abra `<id>` (ou TraceQL abaixo): `GET /…` → `SOAP …/NotificationRead` (`enduser.id`, `soap.attempts`) →
  `soap.attempt` → `POST` ao SAP.
* No Grafana: log → *Abrir trace*; span → botão *Logs*.

```logql
# requisições de um usuário
{service_name="sap-gateway"} | username="alice" |= "http request completed"
# volume por usuário e rota
sum by (username, http_route) (count_over_time({service_name="sap-gateway"} |= "http request completed" | username!="" [1h]))
# falhas SOAP de um usuário numa operação
{service_name="sap-gateway"} | username="alice" | soap_operation="NotificationRead" | error_type!=""
# latência p95 por usuário (ms)
quantile_over_time(0.95, {service_name="sap-gateway"} |= "http request completed" | unwrap duration_ms [5m]) by (username)
```
```traceql
{ span.enduser.id = "alice" && kind = server }
{ status = error && span.enduser.id =~ "alice|bob" }
{ span.soap.operation = "NotificationRead" && span.soap.attempts > 1 }
{ kind = server && duration > 1s }
```
```promql
histogram_quantile(0.95, sum by (le, http_route) (rate(http_server_request_duration_seconds_bucket[5m])))
100 * sum by (soap_operation) (rate(soap_client_calls_total{error_type=~".+"}[5m])) / sum by (soap_operation) (rate(soap_client_calls_total[5m]))
sum by (soap_operation, error_type) (increase(soap_client_retries_total[15m]))
```

**Roteiro de falha**: (1) *Investigação de falhas* → pico por período; (2) filtre por usuário/rota/operação/`error_type`;
(3) abra o log → trace → veja em qual tentativa/qual HTTP do SAP falhou (`soap.attempt` com `error.type`); (4) `error.type`
indica a categoria (tabela de erros). Disponibilidade: `app_health_status`.

## Testes, Ruff e qualidade

```bash
uv run pytest                    # unit + integração (usa o mock SAP em processo; sem Docker)  → 194 passam, 4 e2e pulados
uv run ruff check . && uv run ruff format --check .
uv run mypy src                  # tipagem estática
RUN_E2E=1 uv run pytest tests/e2e -v   # com `docker compose up -d`: prova a chegada dos 3 sinais ao Alloy e ao Tempo/Loki/Prometheus
```

| Camada | Cobertura |
|---|---|
| `tests/unit` | config; aliases/serialização/validação; tri-estado XML; Zeep→estrutura (namespaces/prefixos, repetidos, vazios, nil); mapeadores e exaustividade de códigos; erros→HTTP; retry/backoff; sanitização; transporte e anti-SSRF; auth |
| `tests/integration` | rotas FastAPI contra o mock real (Zeep + requests); encaminhamento Basic/WS-Security; isolamento e concorrência; timeouts, conexão recusada, 503, XML inválido, WSDL indisponível/remoto; retries por idempotência; traces/métricas/logs em exporters em memória; usuário em logs/traces e ausente das métricas; ausência de senha em tudo; **exportação OTLP real** verificada num receptor gRPC e num HTTP/protobuf locais; coletor fora do ar |
| `tests/e2e` | API → Alloy → Tempo/Loki/Prometheus reais, consultando as APIs dos backends |

## Privacidade e retenção

* **Atributos que identificam pessoas**: `username` (logs, Loki structured metadata) e `enduser.id` (spans). Nunca em
  métricas.
* Defina quem acessa o dashboard *Uso por usuário*, Loki e Tempo (Grafana: permissões por pasta/datasource) e a
  retenção compatível com sua política de privacidade/LGPD. No ambiente local: Loki 168 h, Tempo 72 h, Prometheus 7 d.
* Não são registrados corpos SOAP, senhas, `Authorization`, cookies nem tokens. Se o conteúdo de notas for sensível,
  mantenha-o fora de logs (a API só registra metadados).

## Validação realizada e limitações conhecidas

**Validado de verdade (neste ambiente)**: suíte automatizada; stack real (Alloy 1.20.1, Tempo 2.9.0, Loki 3.7.8,
Prometheus 3.15.0, Grafana 13.2.3) com a API no host e em container: os três sinais chegam ao Alloy e aos backends;
as **46 consultas dos dashboards** foram executadas via API do Grafana contra dados reais; correlação **log → trace**
(clique em *Abrir trace*) e **trace → logs** (botão *Logs*) exercitadas no navegador; shutdown por `SIGTERM` com flush.

**Limitações / pendências**

* **Sem SAP real**: o WSDL é de exemplo; faults, códigos e particularidades de um SAP concreto (ex.: BAPI, `Z*`,
  XML com atributos, `choice`, `xsd:any`) exigem ajuste de contratos e, se houver atributos XML/`anyType`, evolução de `plain.py`.
* O **Zeep não documenta thread-safety** do `Document` compartilhado; adotamos uso somente-leitura e há teste com 120 requisições
  concorrentes, mas não é garantia formal. Alternativa: um `Document` por worker/thread.
* A API **não valida a senha** (o SAP valida): um usuário inválido consome uma chamada SOAP. Considere *rate limiting* no gateway/WAF.
* Não há limite de tamanho de resposta SOAP nem rate limiting; `trust_env=False` ignora proxies do ambiente (use `PROXY_URL`).
* `OTEL_EXPORTER_OTLP_*` específicos por sinal e `OTEL_RESOURCE_ATTRIBUTES` além do básico não foram expostos; só as variáveis da tabela.
* `service.instance.id` aleatório por processo gera séries novas a cada reinício (churn esperado no Prometheus).
* Compose é de **desenvolvimento** (Tempo como root, Grafana `admin/admin`, sem TLS/autenticação nos backends, sem alertas).
* A imagem Docker foi construída e executada aqui, mas com um Dockerfile auxiliar que confia no CA do proxy do sandbox
  (o proxy re-termina TLS); `docker compose --profile app build` direto não pôde ser exercitado neste ambiente.
* “Disponibilidade operacional” usa o gauge `app_health_status`; ausência de dados = processo fora do ar (não há `up`
  porque as métricas chegam por remote write).
* WS-Security: apenas `UsernameToken` com senha em texto (exigir https); sem assinatura/criptografia de mensagem.
