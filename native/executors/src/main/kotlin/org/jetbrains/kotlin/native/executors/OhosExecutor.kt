/*
 * Copyright 2010-2025 JetBrains s.r.o. and Kotlin Programming Language contributors.
 * Use of this source code is governed by the Apache 2.0 license that can be found in the license/LICENSE.txt file.
 */

package org.jetbrains.kotlin.native.executors

import java.io.File
import java.io.ByteArrayOutputStream
import java.nio.file.Paths
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.locks.ReentrantLock
import kotlin.time.Duration.Companion.seconds
import kotlin.time.ExperimentalTime

/**
 * [Executor] that sends test to execute on ohos device via hdc.
 */
@OptIn(ExperimentalTime::class)
class OhosExecutor : Executor {
    private val hostExecutor: Executor = HostExecutor()
    private val ldPreload = "LD_PRELOAD=/data/app/el1/bundle/public/com.huawei.hmos.location/libs/arm64/libc++_shared.so"
    private val ldLibraryPath = "LD_LIBRARY_PATH="
    private val exitCodeIndicator = "hdc_shell_exit_code: "

    // 为每个可执行文件维护一个锁，确保同一文件的上传和执行操作串行化
    // 这样可以避免多个线程同时操作同一个文件时出现 I/O 冲突
    private val fileLocks = ConcurrentHashMap<String, ReentrantLock>()

    override fun execute(request: ExecuteRequest): ExecuteResponse {
        val localExePath = request.executableAbsolutePath
        val localExeFile = File(localExePath)

        // 为每个可执行文件获取锁，确保同一文件的操作串行化
        val fileLock = fileLocks.computeIfAbsent(localExePath) { ReentrantLock() }

        fileLock.lock()
        try {
            val deviceExecFdr = "/data/local/tmp/${(1..64).map { ('a'..'z').random() }.joinToString("")}/"
            val deviceExePath = "${deviceExecFdr}${localExeFile.name}"
            val workingDirectory = request.workingDirectory ?: localExeFile.parentFile

            // 1. 创建目录并发送文件
            executeHdcCommand("shell", "rm", "-rf", deviceExecFdr.trimEnd('/'))
            executeHdcCommand("shell", "mkdir", "-p", deviceExecFdr)

            // 2. 发送文件（使用固定超时时间，足够大文件使用）
            executeHdcCommand(
                ExecuteRequest(
                    executableAbsolutePath = "hdc",
                    args = mutableListOf("file", "send", localExePath, deviceExecFdr),
                    workingDirectory = Paths.get("").toAbsolutePath().toFile(),
                    timeout = 20.seconds // 固定120秒，足够大文件使用
                )
            )

            // 3. 同步文件系统并设置权限
            executeHdcCommand("shell", "sync")
            Thread.sleep(300) // 增加等待时间，确保文件系统完全同步

            // 4. 设置权限并验证（最多重试3次）
            for (retry in 0..2) {
                executeHdcCommand("shell", "chmod", "a+x", deviceExePath)
                executeHdcCommand("shell", "sync")
                Thread.sleep(200)

                // 验证权限是否设置成功
                val checkOutput = ByteArrayOutputStream()
                val checkResp = executeHdcCommand(
                    ExecuteRequest(
                        executableAbsolutePath = "hdc",
                        args = mutableListOf("shell", "test", "-x", deviceExePath, "&&", "echo", "ok"),
                        workingDirectory = workingDirectory,
                        stdout = checkOutput,
                        timeout = 5.seconds
                    )
                )
                if (checkResp.exitCode == 0 && checkOutput.toString().contains("ok")) {
                    break
                }
            }

            // 5. 执行程序
            val args = mutableListOf("shell", ldPreload, "${ldLibraryPath}${deviceExecFdr}", deviceExePath)
            args.addAll(request.args)

            val resp = executeHdcCommand(
                ExecuteRequest(
                    executableAbsolutePath = "hdc",
                    args = args,
                    workingDirectory = workingDirectory,
                    stdin = request.stdin,
                    stdout = request.stdout,
                    stderr = request.stderr,
                    environment = request.environment,
                    timeout = request.timeout
                )
            )
            // 清理临时文件
            executeHdcCommand("shell", "rm", "-rf", deviceExecFdr.trimEnd('/'))

            return resp
        } finally {
            fileLock.unlock()
        }
    }

    private fun executeHdcCommand(vararg args: String) {
        val request = ExecuteRequest(
            executableAbsolutePath = "hdc",
            args = args.toMutableList(),
            workingDirectory = Paths.get("").toAbsolutePath().toFile(),
            timeout = 10.seconds
        )
        executeHdcCommand(request)
    }

    private fun executeHdcCommand(request: ExecuteRequest): ExecuteResponse {
        // hdc shell command's exit code isn't that of the command executed in device shell
        if (request.args.getOrNull(0) == "shell") {
            request.args.add(1, "\"")
            // TODO: will trailing whitespace be considered difference between outputs? @linhandev
            request.args.addAll(listOf(";", "echo", exitCodeIndicator, "$?", "\""))
        }
        val capture = ByteArrayOutputStream()
        val filtered = FilteredOutputStream(request.stdout, capture, exitCodeIndicator)
        val resp = hostExecutor.execute(request.copying { stdout = filtered })
        if (request.args.getOrNull(0) == "shell") {
            val realExitCode = capture.toString().lineSequence().lastOrNull {
                it.contains(exitCodeIndicator)
            }?.substringAfter(exitCodeIndicator)?.trim()?.toIntOrNull() ?: -1
            if (realExitCode != resp.exitCode) {
                return resp.copy(exitCode = realExitCode)
            }
        }
        return resp
    }

    private class FilteredOutputStream(
        private val out: java.io.OutputStream?,
        private val capture: ByteArrayOutputStream,
        private val filterPattern: String,
    ) :
        java.io.OutputStream() {
        private val buffer = ByteArrayOutputStream()
        override fun write(x: Int) {
            buffer.write(x); if (x == '\n'.code) flush()
        }

        override fun write(buf: ByteArray, off: Int, len: Int) {
            for (i in off until off + len) write(buf[i].toInt())
        }

        override fun flush() {
            val line = buffer.toString(Charsets.UTF_8.name())
            capture.write(buffer.toByteArray())
            if (!line.contains(filterPattern)) {
                out?.write(buffer.toByteArray())
                out?.flush()
            }
            buffer.reset()
        }
    }
}