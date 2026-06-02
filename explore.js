const puppeteer = require('puppeteer');
const fs = require('fs');

(async () => {
  console.log('Chrome 브라우저를 실행합니다...');

  const browser = await puppeteer.launch({
    headless: false,
    defaultViewport: { width: 1280, height: 900 },
    args: ['--no-sandbox', '--disable-setuid-sandbox']
  });

  const page = await browser.newPage();

  console.log('agrion.kr 접속 중...');
  await page.goto('https://www.agrion.kr/portal/farm/diaryDetail.do', {
    waitUntil: 'networkidle2',
    timeout: 30000
  });

  console.log('현재 URL:', page.url());
  console.log('\n========================================');
  console.log('브라우저에서 로그인해주세요! (최대 3분 대기)');
  console.log('========================================\n');

  // Wait for login to complete by checking if login form disappears
  const maxWait = 180000;
  const startTime = Date.now();
  let loggedIn = false;

  while (Date.now() - startTime < maxWait) {
    await new Promise(r => setTimeout(r, 3000));
    try {
      const hasLoginForm = await page.evaluate(() => {
        const memberIdInput = document.querySelector('#memberId');
        const pwdInput = document.querySelector('#pwd');
        return !!(memberIdInput && pwdInput);
      });

      if (!hasLoginForm) {
        console.log('로그인 완료 감지!');
        loggedIn = true;
        break;
      }
      const elapsed = Math.floor((Date.now() - startTime) / 1000);
      console.log(`로그인 대기 중... (${elapsed}초 경과)`);
    } catch (e) {
      // Page might be navigating after login
      await new Promise(r => setTimeout(r, 2000));
      console.log('페이지 전환 중...');
    }
  }

  if (!loggedIn) {
    console.log('로그인 대기 시간 초과!');
    await browser.close();
    process.exit(1);
  }

  // Wait for page to stabilize after login
  await new Promise(r => setTimeout(r, 3000));
  console.log('현재 URL:', page.url());

  // Navigate to diary creation page
  console.log('\n작업일지 작성 페이지로 이동 중...');
  await page.goto('https://www.agrion.kr/portal/farm/diaryDetail.do', {
    waitUntil: 'networkidle2',
    timeout: 30000
  });
  await new Promise(r => setTimeout(r, 3000));
  console.log('현재 URL:', page.url());

  // Dump the page HTML
  const html = await page.content();
  fs.writeFileSync('/gonggo-radar/page_dump.html', html);
  console.log('페이지 HTML 저장 완료');

  // Comprehensive form analysis
  const formInfo = await page.evaluate(() => {
    const info = {};

    // All forms
    const forms = document.querySelectorAll('form');
    info.formCount = forms.length;
    info.forms = [];

    forms.forEach((form, i) => {
      const formData = {
        id: form.id,
        name: form.name,
        action: form.action,
        method: form.method,
        className: form.className
      };
      info.forms.push(formData);
    });

    // All selects
    info.allSelects = [];
    document.querySelectorAll('select').forEach(select => {
      const options = [];
      select.querySelectorAll('option').forEach(opt => {
        options.push({ value: opt.value, text: opt.textContent.trim() });
      });
      info.allSelects.push({
        name: select.name,
        id: select.id,
        className: (select.className || '').substring(0, 150),
        options: options.slice(0, 50),
        parentLabel: select.closest('tr, div, label')?.querySelector('th, label, .label')?.textContent?.trim() || ''
      });
    });

    // All inputs
    info.allInputs = [];
    document.querySelectorAll('input').forEach(input => {
      info.allInputs.push({
        type: input.type,
        name: input.name,
        id: input.id,
        value: input.value,
        placeholder: input.placeholder,
        className: (input.className || '').substring(0, 150),
        readOnly: input.readOnly,
        disabled: input.disabled,
        parentLabel: input.closest('tr, div, label')?.querySelector('th, label, .label')?.textContent?.trim() || ''
      });
    });

    // All textareas
    info.allTextareas = [];
    document.querySelectorAll('textarea').forEach(ta => {
      info.allTextareas.push({
        name: ta.name,
        id: ta.id,
        value: ta.value,
        className: (ta.className || '').substring(0, 150),
        parentLabel: ta.closest('tr, div, label')?.querySelector('th, label, .label')?.textContent?.trim() || ''
      });
    });

    // All buttons and links with onclick
    info.buttons = [];
    document.querySelectorAll('button, a[onclick], input[type="button"], input[type="submit"]').forEach(el => {
      const text = el.textContent?.trim() || el.value || '';
      if (text.length > 0 && text.length < 100) {
        info.buttons.push({
          tag: el.tagName,
          type: el.type || '',
          text: text,
          id: el.id,
          className: (el.className || '').substring(0, 100),
          href: el.href || '',
          onclick: el.getAttribute('onclick') || ''
        });
      }
    });

    // Date-related elements
    info.dateElements = [];
    document.querySelectorAll('[class*="date"], [id*="date"], [name*="date"], [class*="calendar"], [id*="cal"], [class*="datepicker"], [name*="Date"], [id*="Date"]').forEach(el => {
      info.dateElements.push({
        tag: el.tagName,
        id: el.id,
        name: el.name || '',
        className: (el.className || '').substring(0, 150),
        value: el.value || '',
        type: el.type || ''
      });
    });

    // Table headers (th) - often describe form fields in Korean gov sites
    info.tableHeaders = [];
    document.querySelectorAll('th').forEach(th => {
      info.tableHeaders.push(th.textContent.trim());
    });

    info.title = document.title;
    info.bodyText = document.body?.innerText?.substring(0, 3000);

    return info;
  });

  fs.writeFileSync('/gonggo-radar/form_info.json',
    JSON.stringify(formInfo, null, 2));

  console.log('\n========== 분석 결과 ==========');
  console.log('페이지 제목:', formInfo.title);
  console.log('폼 수:', formInfo.formCount);

  if (formInfo.forms.length > 0) {
    console.log('\n--- Forms ---');
    formInfo.forms.forEach((f, i) => {
      console.log(`  Form ${i}: id=${f.id} name=${f.name} action=${f.action} method=${f.method}`);
    });
  }

  console.log('\n--- Table Headers (form labels) ---');
  console.log('  ', formInfo.tableHeaders.join(' | '));

  console.log('\n--- Selects ---');
  formInfo.allSelects?.forEach(s => {
    console.log(`  [${s.name || s.id}] label="${s.parentLabel}"`);
    console.log(`    options: ${s.options.map(o => `${o.value}="${o.text}"`).join(', ')}`);
  });

  console.log('\n--- Visible Inputs ---');
  formInfo.allInputs?.filter(i => i.type !== 'hidden').forEach(i => {
    console.log(`  [${i.type}] name=${i.name} id=${i.id} value="${i.value}" label="${i.parentLabel}" class="${i.className}"`);
  });

  console.log('\n--- Hidden Inputs ---');
  formInfo.allInputs?.filter(i => i.type === 'hidden').forEach(i => {
    console.log(`  name=${i.name} id=${i.id} value="${i.value}"`);
  });

  console.log('\n--- Textareas ---');
  formInfo.allTextareas?.forEach(t => {
    console.log(`  name=${t.name} id=${t.id} label="${t.parentLabel}" class="${t.className}"`);
  });

  console.log('\n--- Date Elements ---');
  formInfo.dateElements?.forEach(d => {
    console.log(`  [${d.tag}] id=${d.id} name=${d.name} value="${d.value}" class="${d.className}"`);
  });

  console.log('\n--- Buttons ---');
  formInfo.buttons?.forEach(b => {
    console.log(`  [${b.tag}] "${b.text}" id=${b.id} onclick=${b.onclick}`);
  });

  console.log('\n--- Body Text ---');
  console.log(formInfo.bodyText?.substring(0, 2000));

  // Take screenshots
  await page.screenshot({ path: '/gonggo-radar/page_screenshot.png', fullPage: true });
  console.log('\n스크린샷 저장 완료');

  console.log('\n15초 후 브라우저를 닫습니다...');
  await new Promise(r => setTimeout(r, 15000));

  await browser.close();
  console.log('완료!');
})();
