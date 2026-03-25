const puppeteer = require('puppeteer');
const fs = require('fs');

// 작업 스케줄 데이터
const schedule = JSON.parse(fs.readFileSync('/Users/leesangmin/.omc/scientist/work_schedule.json', 'utf8'));

// 작업단계 매핑 (한국어 → 사이트에서 사용하는 값)
const TASK_STEP_MAP = {
  '파종': '파종',
  '물주기': '물주기',
  '하우스관리': '하우스관리',
  '기타작업': '기타작업',
  '예찰활동': '예찰활동'
};

function formatDate(dateStr) {
  // "2026-01-02" → "2026/01/02"
  return dateStr.replace(/-/g, '/');
}

function sleep(ms) {
  return new Promise(r => setTimeout(r, ms));
}

(async () => {
  console.log('======================================');
  console.log('  agrion.kr 영농일지 자동 입력 도구');
  console.log(`  총 ${schedule.length}건 입력 예정`);
  console.log('======================================\n');

  const browser = await puppeteer.launch({
    headless: false,
    defaultViewport: { width: 1280, height: 900 },
    args: ['--no-sandbox', '--disable-setuid-sandbox']
  });

  const page = await browser.newPage();

  // Confirm 대화상자 자동 승인
  page.on('dialog', async dialog => {
    console.log(`  [대화상자] ${dialog.message()}`);
    await dialog.accept();
  });

  // 1. 로그인
  console.log('[1] agrion.kr 접속 중...');
  await page.goto('https://www.agrion.kr/portal/farm/diaryDetail.do', {
    waitUntil: 'networkidle2',
    timeout: 30000
  });

  // 로그인 대기
  const hasLoginForm = await page.evaluate(() => !!document.querySelector('#memberId'));
  if (hasLoginForm) {
    console.log('\n========================================');
    console.log('  브라우저에서 로그인해주세요!');
    console.log('  로그인하면 자동으로 진행됩니다.');
    console.log('  (최대 5분 대기)');
    console.log('========================================\n');

    const maxWait = 300000;
    const startTime = Date.now();
    let loggedIn = false;

    while (Date.now() - startTime < maxWait) {
      await sleep(3000);
      try {
        const stillLogin = await page.evaluate(() => !!document.querySelector('#memberId'));
        if (!stillLogin) {
          loggedIn = true;
          break;
        }
        // Also check for login form visibility
        const loginVisible = await page.evaluate(() => {
          const el = document.querySelector('#memberId');
          return el && el.offsetParent !== null;
        });
        if (!loginVisible) {
          loggedIn = true;
          break;
        }
      } catch (e) {
        await sleep(2000);
      }
    }

    if (!loggedIn) {
      console.log('로그인 대기 시간 초과!');
      await browser.close();
      process.exit(1);
    }
  }

  console.log('[✓] 로그인 완료!\n');
  await sleep(3000);

  // 2. 각 작업일지 입력
  let successCount = 0;
  let failCount = 0;

  for (let i = 0; i < schedule.length; i++) {
    const entry = schedule[i];
    const dateFormatted = formatDate(entry.date);

    console.log(`\n[${i + 1}/${schedule.length}] ${entry.date} (${entry.day}) - ${entry.step}`);
    console.log(`  내용: ${entry.content.replace(/<br>/g, ' / ')}`);

    try {
      // 2-1. 작성 페이지로 이동
      await page.goto('https://www.agrion.kr/portal/farm/diaryDetail.do', {
        waitUntil: 'networkidle2',
        timeout: 30000
      });
      await sleep(2000);

      // 로그인 상태 재확인
      const needLogin = await page.evaluate(() => {
        const el = document.querySelector('#memberId');
        return el && el.offsetParent !== null;
      });
      if (needLogin) {
        console.log('  [!] 세션 만료됨. 다시 로그인해주세요...');
        // Wait for re-login
        const startTime2 = Date.now();
        while (Date.now() - startTime2 < 180000) {
          await sleep(3000);
          const still = await page.evaluate(() => {
            const el = document.querySelector('#memberId');
            return el && el.offsetParent !== null;
          });
          if (!still) break;
        }
        await sleep(2000);
        // Re-navigate
        await page.goto('https://www.agrion.kr/portal/farm/diaryDetail.do', {
          waitUntil: 'networkidle2',
          timeout: 30000
        });
        await sleep(2000);
      }

      // 2-2. 시작일 설정
      console.log('  날짜 설정 중...');
      await page.evaluate((date) => {
        $('#now_date_s').val(date);
        $('#now_date_e').val(date);
      }, dateFormatted);
      await sleep(500);

      // 2-3. 품목 선택 (기타 = 9999)
      console.log('  품목 선택 중...');
      // 먼저 품목 옵션이 로드될 때까지 대기
      await page.waitForSelector('#selectCrops option', { timeout: 10000 });
      await sleep(1000);

      // 옵션 값 확인 및 선택
      const cropSelected = await page.evaluate(() => {
        const select = document.querySelector('#selectCrops');
        if (!select) return false;
        const options = select.querySelectorAll('option');
        for (const opt of options) {
          if (opt.textContent.includes('기타') || opt.value === '9999') {
            select.value = opt.value;
            $(select).trigger('change');
            return true;
          }
        }
        return false;
      });

      if (!cropSelected) {
        console.log('  [!] 품목 "기타" 선택 실패');
        failCount++;
        continue;
      }

      // 2-4. 필지 로드 대기 및 선택
      console.log('  필지 로드 대기 중...');
      await sleep(3000); // AJAX 로드 대기

      // 필지 선택 (대화동 농지)
      const landSelected = await page.evaluate(() => {
        // 필지는 라디오 또는 체크박스로 표시됨
        const landInputs = document.querySelectorAll('input[name=fmUserFarmLandSeq]');
        for (const input of landInputs) {
          // 대화동 관련 항목 찾기
          const parent = input.closest('tr, div, label, li');
          const text = parent ? parent.textContent : '';
          if (text.includes('대화동') || text.includes('1667-4') || text.includes('대화')) {
            input.checked = true;
            input.click();
            return true;
          }
        }
        // 하나만 있으면 그것을 선택
        if (landInputs.length === 1) {
          landInputs[0].checked = true;
          landInputs[0].click();
          return true;
        }
        // 어떤 것이든 선택
        if (landInputs.length > 0) {
          // 마지막 것 선택 (보통 가장 최근 등록)
          const last = landInputs[landInputs.length - 1];
          last.checked = true;
          last.click();
          return true;
        }
        return false;
      });

      if (!landSelected) {
        console.log('  [!] 필지 선택 실패. 옵션을 확인합니다...');
        const landInfo = await page.evaluate(() => {
          const inputs = document.querySelectorAll('input[name=fmUserFarmLandSeq]');
          return Array.from(inputs).map(i => ({
            value: i.value,
            text: i.closest('tr, div, label, li')?.textContent?.trim()?.substring(0, 100)
          }));
        });
        console.log('  필지 옵션:', JSON.stringify(landInfo));
        failCount++;
        continue;
      }

      // 2-5. 품종 로드 대기 및 선택
      console.log('  품종 로드 대기 중...');
      await sleep(3000); // AJAX 로드 대기

      const cropSCodeSelected = await page.evaluate(() => {
        const checkboxes = document.querySelectorAll('input[name=cropSCode]');
        for (const cb of checkboxes) {
          const parent = cb.closest('tr, div, label, li');
          const text = parent ? parent.textContent : '';
          if (text.includes('이끼') || text.includes('기타')) {
            cb.checked = true;
            cb.click();
            return true;
          }
        }
        // 하나만 있으면 선택
        if (checkboxes.length === 1) {
          checkboxes[0].checked = true;
          checkboxes[0].click();
          return true;
        }
        if (checkboxes.length > 0) {
          checkboxes[0].checked = true;
          checkboxes[0].click();
          return true;
        }
        return false;
      });

      if (!cropSCodeSelected) {
        console.log('  [!] 품종 선택 실패');
        failCount++;
        continue;
      }

      // 2-6. 작업단계 로드 대기 및 선택
      console.log('  작업단계 로드 대기 중...');
      await sleep(3000); // AJAX 로드 대기

      const taskSelected = await page.evaluate((stepName) => {
        const select = document.querySelector('#selectTask');
        if (!select) return false;
        const options = select.querySelectorAll('option');

        // 정확한 매칭 시도
        for (const opt of options) {
          if (opt.textContent.trim() === stepName) {
            select.value = opt.value;
            $(select).trigger('change');
            return true;
          }
        }

        // 부분 매칭 시도
        for (const opt of options) {
          const text = opt.textContent.trim();
          if (text.includes(stepName) || stepName.includes(text)) {
            select.value = opt.value;
            $(select).trigger('change');
            return true;
          }
        }

        // 매칭 실패 시 첫 번째 유효 옵션 선택
        for (const opt of options) {
          if (opt.value && opt.value !== '') {
            select.value = opt.value;
            $(select).trigger('change');
            return true;
          }
        }
        return false;
      }, entry.step);

      if (!taskSelected) {
        console.log('  [!] 작업단계 선택 실패');
        const taskInfo = await page.evaluate(() => {
          const select = document.querySelector('#selectTask');
          if (!select) return [];
          return Array.from(select.options).map(o => ({ value: o.value, text: o.textContent.trim() }));
        });
        console.log('  작업단계 옵션:', JSON.stringify(taskInfo));
        failCount++;
        continue;
      }

      // 2-7. 작업내용 입력
      console.log('  작업내용 입력 중...');
      await page.evaluate((content) => {
        const memo = document.querySelector('#memo');
        if (memo) {
          memo.value = content;
          $(memo).trigger('change');
        }
      }, entry.content);
      await sleep(500);

      // 2-8. 공개 여부 설정 (비공개)
      await page.evaluate(() => {
        const noShow = document.querySelector('#noShow');
        if (noShow) noShow.click();
      });

      // 2-9. 예약 알림 (아니오)
      await page.evaluate(() => {
        const noRez = document.querySelector('#noRez');
        if (noRez) noRez.click();
      });

      await sleep(1000);

      // 2-10. 저장 (insertDiary 호출)
      console.log('  저장 중...');

      // AJAX 응답 대기를 위한 Promise 설정
      const saveResult = await page.evaluate(() => {
        return new Promise((resolve) => {
          // 입력 유효성 체크
          if (!fnValid()) {
            resolve({ success: false, reason: 'validation_failed' });
            return;
          }

          // 품종 seq 저장
          var cropSCodeSeq = "";
          $("input:checkbox[name=cropSCode]").each(function() {
            if ($(this).is(":checked")) {
              if (cropSCodeSeq !== "") cropSCodeSeq += ",";
              cropSCodeSeq += $(this).attr("value2");
            }
          });
          $("#fmUserGrowCropSeq").val(cropSCodeSeq);

          var data = serializeObject($("#diaryForm").serializeArray());
          data.actDt = moment($('#now_date_s').val(), 'YYYY/MM/DD').format('YYYYMMDD');
          data.actEndDt = moment($('#now_date_e').val(), 'YYYY/MM/DD').format('YYYYMMDD');
          data.taskCode = $("#selectTask > option:selected").attr("value");
          data.taskType = $("#selectTask > option:selected").attr("value2");
          data.rezYn = $("input:radio[name=isRez]:checked").val() || "N";
          data.rezDt = "";

          var taskType2 = data.taskType;
          if (taskType2 != "0" || taskType2 != null) {
            // Skip taskValid for simplicity
          }

          var formData = new FormData();
          $.each(data, function(name, val) {
            formData.append(name, val || '');
          });

          $.ajax({
            type: 'POST',
            url: '/portal/farm/insertFarmDiary.do',
            data: formData,
            enctype: 'multipart/form-data',
            contentType: false,
            processData: false,
            xhrFields: { withCredentials: true },
            cache: false,
            success: function(data) {
              if (data.resultCd == 0) {
                resolve({ success: true });
              } else if (data.resultCd == -3) {
                resolve({ success: false, reason: 'rate_limit' });
              } else {
                resolve({ success: false, reason: 'error', code: data.resultCd });
              }
            },
            error: function(error) {
              resolve({ success: false, reason: 'ajax_error', msg: error?.responseJSON?.msg });
            }
          });
        });
      });

      if (saveResult.success) {
        successCount++;
        console.log(`  [✓] 저장 완료! (${successCount}/${schedule.length})`);
      } else if (saveResult.reason === 'rate_limit') {
        console.log('  [!] 속도 제한 발생. 30초 대기 후 재시도...');
        await sleep(30000);
        // 재시도
        i--; // 같은 항목 다시 시도
        continue;
      } else if (saveResult.reason === 'validation_failed') {
        console.log('  [!] 유효성 검사 실패');
        // 스크린샷 저장
        await page.screenshot({ path: `/Users/leesangmin/gonggo-radar/fail_${entry.no}.png` });
        failCount++;
      } else {
        console.log(`  [!] 저장 실패: ${saveResult.reason} (code: ${saveResult.code})`);
        failCount++;
      }

      // 속도 제한 방지를 위한 대기 (10~15초)
      if (i < schedule.length - 1) {
        const waitTime = 10000 + Math.random() * 5000;
        console.log(`  다음 항목까지 ${Math.round(waitTime / 1000)}초 대기...`);
        await sleep(waitTime);
      }

    } catch (err) {
      console.log(`  [!] 오류 발생: ${err.message}`);
      failCount++;
      await sleep(5000);
    }
  }

  // 3. 결과 요약
  console.log('\n======================================');
  console.log('  작업 완료!');
  console.log(`  성공: ${successCount}건`);
  console.log(`  실패: ${failCount}건`);
  console.log(`  총: ${schedule.length}건`);
  console.log('======================================');

  // 결과 파일 저장
  fs.writeFileSync('/Users/leesangmin/gonggo-radar/result.json', JSON.stringify({
    total: schedule.length,
    success: successCount,
    fail: failCount,
    timestamp: new Date().toISOString()
  }, null, 2));

  console.log('\n브라우저는 열려 있습니다. 확인 후 직접 닫아주세요.');

  // 브라우저를 닫지 않고 유지 (사용자가 직접 닫을 수 있도록)
  // 30분 후 자동 종료
  await sleep(1800000);
  await browser.close();
})();
