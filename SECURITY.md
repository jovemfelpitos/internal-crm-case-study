# Segurança e publicação

## Conteúdo público

O código foi preparado em uma cópia separada. A publicação usa uma lista explícita de fontes e exclui bancos de dados, planilhas, backups, logs, credenciais, arquivos de implantação e capturas operacionais.

Os usuários e equipes iniciais são fictícios. E-mails de demonstração usam o domínio reservado `example.invalid`. O banco inicial não contém clientes; os clientes dos testes são gerados em memória.

## Configuração local

- Defina `INITIAL_LOCAL_PASSWORD` antes de inicializar as contas de demonstração.
- A aplicação exige uma senha com pelo menos oito caracteres e recusa senhas comuns.
- Na primeira execução de desenvolvimento, uma chave de sessão aleatória é criada em `instance/.secret_key`. A pasta `instance/` inteira fica fora do Git.
- O login Google fica desativado sem `GOOGLE_CLIENT_ID` e `GOOGLE_CLIENT_SECRET` próprios. A lista padrão de domínios de cadastro é fictícia.
- O servidor de desenvolvimento deve ser utilizado em `127.0.0.1`.

## Ambiente real

Configure `APP_PRODUCTION=1`, uma `SECRET_KEY` própria, `ALLOWED_HOSTS`, HTTPS e `SESSION_COOKIE_SECURE=1`. Use contas e senhas individuais, permissões adequadas e backup com acesso restrito. O servidor de desenvolvimento do Flask não é um servidor de produção.

A aplicação possui validação de Host, CSRF, cookies defensivos, limitação de tentativas de login e auditoria. Essa descrição não substitui uma avaliação de segurança do ambiente em que o código vier a ser implantado.

## Antes de enviar alterações

Execute:

```bash
python tools/check_publication.py
python -m unittest discover -s tests -p "test_*.py"
```

O verificador bloqueia caminhos de dados operacionais e alguns padrões conhecidos de credenciais nos arquivos rastreados pelo Git. Ele não comprova ausência de todo dado pessoal possível; revise manualmente novos exemplos, documentos e imagens. Nunca publique dados de clientes em arquivos, issues ou comentários.

