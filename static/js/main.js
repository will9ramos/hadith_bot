// --- START OF FILE static/js/main.js ---

/**
 * Вставляет HTML-теги в текстовое поле в позицию курсора
 */
function insertHtmlTag(textareaId, tag) {
    const textarea = document.getElementById(textareaId);
    if (!textarea) return;

    const start = textarea.selectionStart;
    const end = textarea.selectionEnd;
    const selectedText = textarea.value.substring(start, end);
    let replacement = selectedText;

    let prefix = `<${tag}>`;
    let suffix = `</${tag}>`;

    switch (tag) {
        case 'b': // bold
        case 'i': // italic
        case 'u': // underline
        case 's': // strikethrough
        case 'spoiler':
             prefix = tag === 'spoiler' ? '<tg-spoiler>' : `<${tag}>`;
             suffix = tag === 'spoiler' ? '</tg-spoiler>' : `</${tag}>`;
            break;
        case 'code':
            prefix = '<code>';
            suffix = '</code>';
            break;
        case 'pre':
            prefix = '<pre>';
            suffix = '</pre>';
            break;
        case 'blockquote':
             prefix = '<blockquote>';
             suffix = '</blockquote>';
             break;
        case 'a':
            const url = prompt("Введите URL ссылки:", "https://");
            if (url) {
                replacement = `<a href="${url}">${selectedText || 'текст ссылки'}</a>`;
                textarea.value = textarea.value.substring(0, start) + replacement + textarea.value.substring(end);
                textarea.focus();
                textarea.selectionStart = textarea.selectionEnd = start + replacement.length;
                return;
            }
            break;
    }

    if (replacement !== selectedText || selectedText === '') {
         textarea.value = textarea.value.substring(0, start) + prefix + replacement + suffix + textarea.value.substring(end);
    } else {
         textarea.value = textarea.value.substring(0, start) + prefix + selectedText + suffix + textarea.value.substring(end);
    }

    textarea.focus();
    if (selectedText) {
        textarea.selectionStart = start + prefix.length;
        textarea.selectionEnd = end + prefix.length;
    } else {
        textarea.selectionStart = textarea.selectionEnd = start + prefix.length;
    }
}

/**
 * Валидация форм рассылки и загрузки файлов
 */
document.addEventListener('DOMContentLoaded', function() {
    const fileInput = document.getElementById('files');
    const fileIdInput = document.getElementById('file_id');
    const broadcastForm = document.getElementById('broadcastForm');

    // 1. Проверка загружаемых файлов (Рассылка)
    if (fileInput) {
        fileInput.addEventListener('change', function() {
            // А. Проверка количества (макс 10 для альбома)
            if (this.files.length > 10) {
                alert('❌ Ошибка: Telegram API позволяет отправлять максимум 10 файлов в одном альбоме!');
                this.value = ''; // Сброс выбора
                return;
            }

            // Б. Проверка размера каждого файла
            for (let i = 0; i < this.files.length; i++) {
                const file = this.files[i];
                const sizeMB = file.size / (1024 * 1024);
                const ext = file.name.split('.').pop().toLowerCase();
                const isPhoto = ['jpg', 'jpeg', 'png', 'gif'].includes(ext);

                // Лимит для ФОТО: 10 МБ (ограничение Telegram API sendPhoto)
                if (isPhoto && sizeMB > 10) {
                    alert(`❌ Ошибка: Фото "${file.name}" весит ${sizeMB.toFixed(2)} МБ.\nTelegram API не принимает фото больше 10 МБ для отправки как Photo.`);
                    this.value = '';
                    return;
                }

                // Лимит для ОСТАЛЬНОГО: 50 МБ (ограничение Upload API)
                if (sizeMB > 50) {
                    alert(`❌ Ошибка: Файл "${file.name}" весит ${sizeMB.toFixed(2)} МБ.\nБот не может загружать файлы больше 50 МБ.`);
                    this.value = '';
                    return;
                }
            }
        });
    }

    // 2. Проверка формы рассылки перед отправкой
    if (broadcastForm) {
        broadcastForm.addEventListener('submit', function(event) {
            const hasFiles = fileInput && fileInput.files.length > 0;
            const hasFileId = fileIdInput && fileIdInput.value.trim() !== "";

            // Нельзя выбрать и файлы, и File ID одновременно
            if (hasFiles && hasFileId) {
                event.preventDefault();
                alert("❌ Ошибка: Пожалуйста, выберите ТОЛЬКО ОДИН способ отправки медиа: либо загрузите файлы, либо вставьте File ID.");
            }
        });
    }
    
    // 3. Валидация аудио (для страницы загрузки аудио к хадису)
    const audioInput = document.querySelector('input[name="audio_file"]');
    if (audioInput) {
        audioInput.addEventListener('change', function() {
            if (this.files.length > 0) {
                const file = this.files[0];
                const sizeMB = file.size / (1024 * 1024);
                
                if (sizeMB > 50) {
                    alert(`❌ Ошибка: Аудиофайл "${file.name}" весит ${sizeMB.toFixed(2)} МБ.\nМаксимальный размер: 50 МБ.`);
                    this.value = '';
                }
            }
        });
    }
});

// --- END OF FILE static/js/main.js ---