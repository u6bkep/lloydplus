import ghidra.app.script.GhidraScript;
import ghidra.program.model.symbol.*;
import ghidra.program.model.listing.*;
import java.io.*;
public class ApplyNames extends GhidraScript {
    public void run() throws Exception {
        BufferedReader br = new BufferedReader(new FileReader(getScriptArgs()[0]));
        String l;
        while ((l = br.readLine()) != null) {
            String[] p = l.split(" ");
            var a = toAddr(Long.parseLong(p[1], 16));
            if (p[0].equals("L")) { createLabel(a, p[2], true, SourceType.USER_DEFINED); }
            else {
                Function f = getFunctionAt(a);
                if (f == null) f = createFunction(a, null);
                if (f != null && f.getName().startsWith("FUN_")) f.setName(p[2], SourceType.USER_DEFINED);
            }
        }
    }
}
