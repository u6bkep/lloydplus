import ghidra.app.script.GhidraScript;
import ghidra.program.model.symbol.*;
import ghidra.program.model.address.*;
import ghidra.program.model.listing.*;
import java.io.*;

public class Vtables extends GhidraScript {
    @Override
    public void run() throws Exception {
        PrintWriter pw = new PrintWriter(new FileWriter(getScriptArgs()[0]));
        SymbolIterator it = currentProgram.getSymbolTable().getAllSymbols(true);
        while (it.hasNext()) {
            Symbol s = it.next();
            if (!s.getName().equals("vftable")) continue;
            Address a = s.getAddress();
            String cls = s.getParentNamespace().getName(true);
            pw.println("VTABLE " + a + " " + cls);
            for (int i = 0; i < 80; i++) {
                Address e = a.add(i * 4);
                if (i > 0 && currentProgram.getSymbolTable().getPrimarySymbol(e) != null
                    && currentProgram.getSymbolTable().getPrimarySymbol(e).getSource() != SourceType.DEFAULT
                    && !currentProgram.getSymbolTable().getPrimarySymbol(e).getName().startsWith("PTR")) break;
                long v = getInt(e) & 0xffffffffL;
                Address t = toAddr(v);
                Function f = getFunctionAt(t);
                if (f == null) break;
                pw.println("  [" + i + "] " + t + " " + f.getName(true));
            }
        }
        pw.close();
    }
}
