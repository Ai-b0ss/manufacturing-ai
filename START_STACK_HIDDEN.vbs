Option Explicit
Dim shell, fs, folder, command, code, log
Set shell = CreateObject("WScript.Shell")
Set fs = CreateObject("Scripting.FileSystemObject")
folder = fs.GetParentFolderName(WScript.ScriptFullName)
command = "cmd.exe /d /c " & Chr(34) & Chr(34) & folder & "\START_STACK_SAFE.cmd" & Chr(34) & Chr(34)
code = shell.Run(command, 0, True)
Set log = fs.OpenTextFile(folder & "\data\hidden_stack_launcher.log", 8, True)
log.WriteLine Now & " | exit=" & code
log.Close
WScript.Quit code
