describe('Personal Telegram login against the real HTTPS backend', () => {
  function start(scenario = 'phone') {
    return cy.request('POST', '/__fixture/start', { scenario }).its('body.url');
  }

  function phoneCode() {
    cy.intercept('POST', '/login/**').as('loginForm');
    cy.get('input[name=phone]').type('+79990000000');
    cy.contains('button', 'Получить код входа').click();
    cy.wait('@loginForm').its('request.headers.origin').should('eq', 'https://127.0.0.1:8443');
    cy.get('input[name=code]').should('have.attr', 'autocomplete', 'one-time-code');
  }

  it('saves only after consent, then publishes once to two channels through the bot adapter', () => {
    start('2fa').then((url) => cy.visit(url));
    cy.contains('Сессия даёт приложению').should('be.visible');
    phoneCode();
    cy.get('input[name=code]').type('12345', { log: false });
    cy.contains('button', 'Подтвердить код').click();
    cy.get('input[name=password]').should('have.attr', 'type', 'password');
    cy.get('input[name=password]').type('fixture-password', { log: false });
    cy.contains('button', 'Подтвердить пароль').click();
    cy.contains('Сохранить этот аккаунт').should('be.visible');
    cy.request('/__fixture/state').its('body.accounts').should('have.length', 0);
    cy.contains('button', 'Сохранить аккаунт').click();
    cy.contains('Аккаунт подключён').should('be.visible');
    cy.request('/__fixture/state').then(({ body }) => {
      expect(body.accounts).to.have.length(1);
      expect(body.accounts[0].platform).to.eq('telegram-user');
      expect(body.encrypted).to.eq(true);
      expect(body.legacy_empty).to.eq(true);
    });
    cy.request('POST', '/__fixture/publish').then(({ body }) => {
      expect(body.writes).to.deep.eq([
        { peer: 1, text: 'Browser-confirmed news' },
        { peer: 2, text: 'Browser-confirmed news' },
      ]);
      expect(body.status.map((row) => row.state)).to.deep.eq(['sent', 'sent']);
    });
  });

  it('shows QR and permits cancellation without saving an account', () => {
    cy.viewport(1280, 900);
    start('qr').then((url) => cy.visit(url));
    cy.contains('button', 'Показать QR-код').click();
    cy.get('img[alt="QR-код входа в ваш Telegram"]').should('be.visible');
    cy.request('POST', '/__fixture/scan');
    cy.reload();
    cy.contains('button', 'Сохранить аккаунт').should('be.visible');
    cy.contains('button', 'Отменить вход').click();
    cy.contains('Вход отменён').should('be.visible');
    cy.request('/__fixture/state').then(({ body }) => {
      expect(body.accounts).to.have.length(0);
      expect(body.revocations).to.be.greaterThan(0);
    });
  });

  it('reports invalid codes and rejects opening a bound link in another browser', () => {
    start().then((url) => {
      cy.visit(url);
      phoneCode();
      cy.get('input[name=code]').type('99999', { log: false });
      cy.contains('button', 'Подтвердить код').click();
      cy.get('[role=alert]').should('contain', 'Telegram отклонил');
      cy.request('/__fixture/state').its('body.accounts').should('have.length', 0);
      cy.clearCookies();
      cy.visit({ url, failOnStatusCode: false });
      cy.contains('другом браузере').should('be.visible');
    });
  });

  it('rejects an expired authorization link', () => {
    start().then((url) => {
      cy.request('POST', '/__fixture/expire');
      cy.visit({ url, failOnStatusCode: false });
      cy.contains('Ссылка истекла').should('be.visible');
      cy.request('/__fixture/state').its('body.accounts').should('have.length', 0);
    });
  });
});
